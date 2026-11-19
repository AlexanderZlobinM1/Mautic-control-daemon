from dataclasses import dataclass, field


@dataclass(slots=True)
class DBConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    table_prefix: str


@dataclass(slots=True)
class MauticInstall:
    instance_uid: str
    name: str
    root: str
    console_path: str
    instance_code: str | None = None
    primary_domain: str | None = None
    local_php_path: str | None = None
    mautic_timezone: str | None = None
    mautic_major: int | None = None
    db: DBConfig | None = None
    source: str = "autodiscovery"
    markers: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    runtime: str = "host"
    runtime_id: str | None = None
    runtime_root: str | None = None
    runtime_user: str | None = None
    runtime_php_bin: str | None = None
    runtime_image_ref: str | None = None
    install_type: str | None = None
    runtime_capabilities: list[str] = field(default_factory=list)
    runtime_adapter: str | None = None

    def safe_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "instance_uid": self.instance_uid,
            "instance_code": self.instance_code,
            "name": self.name,
            "root": self.root,
            "primary_domain": self.primary_domain,
            "console_path": self.console_path,
            "local_php_path": self.local_php_path,
            "mautic_timezone": self.mautic_timezone,
            "mautic_major": self.mautic_major,
            "source": self.source,
            "markers": self.markers,
            "domains": list(self.domains or []),
            "runtime": self.runtime,
            "runtime_id": self.runtime_id,
            "runtime_root": self.runtime_root,
            "runtime_user": self.runtime_user,
            "runtime_php_bin": self.runtime_php_bin,
            "runtime_image_ref": self.runtime_image_ref,
            "install_type": self.install_type,
            "runtime_capabilities": list(self.runtime_capabilities or []),
            "runtime_adapter": self.runtime_adapter,
        }
        if self.db:
            payload["db"] = {
                "host": self.db.host,
                "port": self.db.port,
                "name": self.db.name,
                "user": self.db.user,
                "table_prefix": self.db.table_prefix,
                "password_masked": "********",
            }
        else:
            payload["db"] = None
        return payload
