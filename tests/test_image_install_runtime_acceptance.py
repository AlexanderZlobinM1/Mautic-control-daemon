from contextlib import nullcontext
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest
from mcd_agent import mautic_image_install as install


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    events = []
    plan = install.build_plan(image_ref="vanila7", domain="verify.example.org", php_version="8.4")
    plan.webroot = tmp_path / "verify" / "public_html"
    cfg = SimpleNamespace(mcc_token="test", mcc_url="https://controller.example.org", state_db_path=str(tmp_path / "state.db"))
    monkeypatch.setattr(install, "build_plan", lambda **kw: plan)
    monkeypatch.setattr(install, "_preflight", lambda plan: [])
    monkeypatch.setattr("mcd_agent.source_operation.source_operation_lease", lambda cfg: nullcontext())
    def download(url, token, destination):
        if destination.name == "files.tar.gz":
            with tarfile.open(destination, "w:gz"):
                pass
        else:
            destination.write_bytes(b"database")
    def extract(archive, root):
        for rel in ["config/local.php", "bin/console"]:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
    monkeypatch.setattr(install, "_download", download)
    monkeypatch.setattr(install, "_safe_extract", extract)
    monkeypatch.setattr(install, "_mysql_exec", lambda *a, **kw: "")
    monkeypatch.setattr(install, "_mysql_import_artifact", lambda *a: events.append("db"))
    monkeypatch.setattr(install, "_patch_local_php", lambda *a: events.append("config"))
    monkeypatch.setattr(install, "_run", lambda *a, **kw: (0, ""))
    monkeypatch.setattr(install, "_write_nginx_vhost", lambda *a, **kw: events.append("vhost"))
    monkeypatch.setattr(install, "_run_certbot_http01", lambda *a: events.append("certificate"))
    monkeypatch.setattr(install, "_warm_image_runtime", lambda *a: events.append("warm") or {"status": "ok"})
    monkeypatch.setattr(install, "_verify_image_https", lambda *a: events.append("https") or {"status": "ok", "http_status": 200})
    monkeypatch.setattr(install, "_rollback_failed_install", lambda *a, **kw: events.append("rollback") or [])
    from mcd_agent import inventory
    monkeypatch.setattr(inventory, "InstanceInventory", lambda *a: SimpleNamespace(rescan=lambda cfg: events.append("rescan") or 1))
    return cfg, plan, events


def run(scenario, **kwargs):
    cfg, _, _ = scenario
    return install.install_from_image(cfg, image_ref="vanila7", domain="verify.example.org", php_version="8.4", yes=True, **kwargs)


def test_publish_and_success_follow_prod_warmup_and_https_acceptance(scenario):
    result = run(scenario)
    events = scenario[2]
    assert events.index("config") < events.index("warm") < events.index("vhost") < events.index("https") < events.index("rescan")
    assert result["runtime_warmup"]["status"] == "ok"
    assert result["runtime_verification"]["http_status"] == 200


def test_warmup_failure_rolls_back_before_vhost_and_inventory(scenario, monkeypatch):
    def fail(plan):
        raise RuntimeError("warmup failed")
    monkeypatch.setattr(install, "_warm_image_runtime", fail)
    with pytest.raises(RuntimeError, match="warmup failed"):
        run(scenario)
    assert "rollback" in scenario[2]
    assert "vhost" not in scenario[2]
    assert "rescan" not in scenario[2]


def test_http_failure_cannot_publish_success(scenario, monkeypatch):
    def fail(plan):
        raise RuntimeError("HTTP 500")
    monkeypatch.setattr(install, "_verify_image_https", fail)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        run(scenario)
    assert "rollback" in scenario[2]
    assert "rescan" not in scenario[2]


def test_mail_cache_mutation_is_followed_by_final_warmup(scenario, monkeypatch):
    from mcd_agent import mail_config
    monkeypatch.setattr(mail_config, "apply_mail_profile", lambda *a, **kw: scenario[2].append("mail"))
    run(scenario, mail_profile_id="configured-profile")
    events = scenario[2]
    assert events.count("warm") == 2
    assert events.index("mail") < len(events) - 1 - events[::-1].index("warm") < events.index("https")


def test_warmup_uses_selected_php_and_instance_runtime_user(tmp_path, monkeypatch):
    plan = install.build_plan(image_ref="any", domain="verify.example.org", php_version="8.4")
    plan.webroot = tmp_path
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/console").touch()
    calls = []
    monkeypatch.setattr(install, "_run", lambda command, **kw: calls.append(command) or (0, ""))
    install._warm_image_runtime(plan)
    assert calls == [["sudo", "-n", "-u", "www-data", "--", "/usr/bin/php8.4", str(tmp_path / "bin/console"), "cache:warmup", "--env=prod", "--no-debug", "--no-interaction"]]


@pytest.mark.parametrize("rc,status,body,accepted", [(0,"200",'<input type="password">',True),(0,"500","error",False),(60,"000","",False),(0,"302","",False),(0,"200","default stub",False)])
def test_https_gate_requires_real_tls_success_200_and_login_form(monkeypatch, rc, status, body, accepted):
    plan = install.build_plan(image_ref="any", domain="verify.example.org", php_version="8.4")
    def command(args, **kw):
        assert "--insecure" not in args and "-k" not in args
        assert "verify.example.org:443:127.0.0.1" in args
        assert args[-1] == "https://verify.example.org/s/login"
        Path(args[args.index("--output")+1]).write_text(body)
        return rc, status
    monkeypatch.setattr(install, "_run", command)
    if accepted:
        assert install._verify_image_https(plan)["tls_verified"] is True
    else:
        with pytest.raises(RuntimeError):
            install._verify_image_https(plan)


def test_warmup_nonzero_is_failure_without_secret_console_output(tmp_path, monkeypatch):
    plan = install.build_plan(image_ref="any", domain="verify.example.org", php_version="8.4")
    plan.webroot = tmp_path
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/console").touch()
    monkeypatch.setattr(install, "_run", lambda *a, **kw: (1, "sensitive imported config"))
    with pytest.raises(RuntimeError, match="prod cache warmup failed rc=1") as exc:
        install._warm_image_runtime(plan)
    assert "sensitive" not in str(exc.value)


def test_failed_ownership_never_warms_or_publishes(scenario, monkeypatch):
    monkeypatch.setattr(install, "_run", lambda *a, **kw: (1, "ownership failed"))
    with pytest.raises(RuntimeError, match="ownership setup failed"):
        run(scenario)
    assert "warm" not in scenario[2]
    assert "vhost" not in scenario[2]
    assert "rollback" in scenario[2]
