from pathlib import Path

from lm3_teleop_bridge import cli
from lm3_teleop_bridge.config import AppConfig, RecordingConfig


def test_serve_cli_does_not_require_token_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = AppConfig(recording=RecordingConfig(root=tmp_path / "raw", fps=20))
    captured: dict[str, object] = {}

    monkeypatch.delenv("LM3_TELEOP_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda path: config)

    async def fake_serve_forever(
        supplied_config: AppConfig,
        *,
        hardware_flag: bool,
        allow_lan_flag: bool,
    ) -> None:
        captured.update(
            config=supplied_config,
            hardware_flag=hardware_flag,
            allow_lan_flag=allow_lan_flag,
        )

    monkeypatch.setattr(cli, "_serve_forever", fake_serve_forever)

    result = cli.main(["serve", "--config", str(tmp_path / "sim.toml")])

    assert result == 0
    assert captured == {
        "config": config,
        "hardware_flag": False,
        "allow_lan_flag": False,
    }
