from pathlib import Path

from mcd_agent.grapesjs_ckeditor_patch import ensure_grapesjs_ckeditor_gpl_patch
from mcd_agent.models import MauticInstall


SOURCE_REL = "Assets/library/js/plugins/grapesjs-ckeditor/editorLifecycle.js"
BUILDER_SERVICE_REL = "Assets/library/js/builder.service.js"
DIST_REL = "Assets/library/js/dist/builder.js"
ASSETS_SUBSCRIBER_REL = "EventSubscriber/AssetsSubscriber.php"


def _install(tmp_path: Path, major: int = 7) -> MauticInstall:
    return MauticInstall(
        instance_uid="example.test",
        name="example.test",
        root=str(tmp_path),
        console_path=str(tmp_path / "bin/console"),
        mautic_major=major,
    )


def _write_fixture(
    tmp_path: Path,
    dist: str | None = None,
) -> tuple[Path, Path, Path, Path]:
    plugin = tmp_path / "docroot/plugins/GrapesJsBuilderBundle"
    source = plugin / SOURCE_REL
    service = plugin / BUILDER_SERVICE_REL
    built = plugin / DIST_REL
    subscriber = plugin / ASSETS_SUBSCRIBER_REL
    source.parent.mkdir(parents=True)
    built.parent.mkdir(parents=True)
    subscriber.parent.mkdir(parents=True)
    source.write_text("const config = { licenseKey: this.licenseKey, toolbar: [] };\n", encoding="utf-8")
    service.write_text(
        "grapesjsmautic: BuilderService.getMauticConf('email-html'),\n"
        "        [grapesjsckeditor]: {\n"
        "          ckeditor_module: ckeditorModuleUrl,\n"
        "          inlineMode: true,\n",
        encoding="utf-8",
    )
    built.write_text(
        dist
        or 'a={licenseKey:this.licenseKey};grapesjsmautic:gl.getMauticConf("email-html"),'
        '[mT]:{ckeditor_module:r,inlineMode:!0',
        encoding="utf-8",
    )
    subscriber.write_text(
        "$assetsEvent->addScript('plugins/GrapesJsBuilderBundle/Assets/library/js/dist/builder.js');\n",
        encoding="utf-8",
    )
    return source, service, built, subscriber


def test_patches_source_and_published_builder_idempotently(tmp_path: Path) -> None:
    source, service, built, subscriber = _write_fixture(tmp_path)

    first = ensure_grapesjs_ckeditor_gpl_patch(_install(tmp_path))
    assert first["status"] == "patched"
    assert "licenseKey: this.licenseKey || 'GPL'," in source.read_text(encoding="utf-8")
    assert "licenseKey: 'GPL'," in service.read_text(encoding="utf-8")
    assert 'licenseKey:this.licenseKey||"GPL"' in built.read_text(encoding="utf-8")
    assert 'ckeditor_module:r,licenseKey:"GPL",inlineMode:!0' in built.read_text(encoding="utf-8")
    assert "builder.js?mcd-ckeditor-gpl" in subscriber.read_text(encoding="utf-8")
    assert source.with_name(source.name + ".mcd-bak").exists()
    assert service.with_name(service.name + ".mcd-bak").exists()
    assert built.with_name(built.name + ".mcd-bak").exists()
    assert subscriber.with_name(subscriber.name + ".mcd-bak").exists()

    second = ensure_grapesjs_ckeditor_gpl_patch(_install(tmp_path))
    assert second["status"] == "already"


def test_refuses_ambiguous_published_signature(tmp_path: Path) -> None:
    _, _, built, _ = _write_fixture(
        tmp_path,
        'a={licenseKey:this.licenseKey};b={licenseKey:this.licenseKey};'
        'grapesjsmautic:gl.getMauticConf("email-html"),[mT]:{ckeditor_module:r,inlineMode:!0',
    )

    result = ensure_grapesjs_ckeditor_gpl_patch(_install(tmp_path))

    assert result["status"] == "error"
    assert built.read_text(encoding="utf-8").count("licenseKey:this.licenseKey") == 2


def test_skips_unknown_newer_signature(tmp_path: Path) -> None:
    source, service, built, subscriber = _write_fixture(tmp_path, "a={licenseKey:options.licenseKey};")
    source.write_text("const config = { licenseKey: options.licenseKey };\n", encoding="utf-8")
    service.write_text("new email builder signature\n", encoding="utf-8")
    subscriber.write_text("new asset subscriber signature\n", encoding="utf-8")

    result = ensure_grapesjs_ckeditor_gpl_patch(_install(tmp_path))

    assert result["status"] == "skip"
    assert all(item["reason"] == "pattern_not_found" for item in result["files"])


def test_skips_non_mautic_7(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    result = ensure_grapesjs_ckeditor_gpl_patch(_install(tmp_path, major=6))

    assert result == {"status": "skip", "reason": "not_mautic_7", "root": str(tmp_path)}
