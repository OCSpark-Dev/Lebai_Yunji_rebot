from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .exporter import ExportError, export_lerobot_v3
from .recorder import verify_manifest
from .server import TeleopServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LM3-UP fail-closed teleoperation bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the WebSocket bridge")
    serve_parser.add_argument("--config", required=True, type=Path)
    serve_parser.add_argument("--hardware", action="store_true", help="explicitly allow hardware backend")
    serve_parser.add_argument("--robot-ip", help="override robot.robot_ip")
    serve_parser.add_argument("--allow-lan", action="store_true", help="explicitly allow non-loopback listen")
    serve_parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))

    export_parser = subparsers.add_parser("export", help="export reviewed raw episodes with LeRobot v0.4.2")
    export_parser.add_argument("--episode", action="append", required=True, type=Path)
    export_parser.add_argument("--output", required=True, type=Path)
    export_parser.add_argument("--repo-id", default="local/lm3_up")
    export_parser.add_argument("--camera", action="append", dest="cameras")
    export_parser.add_argument("--max-image-delta-ms", type=int, default=100)
    export_parser.add_argument("--lerobot-source", type=Path)

    verify_parser = subparsers.add_parser("verify", help="verify raw episode manifests")
    verify_parser.add_argument("episode", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            logging.basicConfig(
                level=getattr(logging, args.log_level),
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
            config = load_config(args.config)
            if args.robot_ip:
                config.robot.robot_ip = args.robot_ip
            config.validate(hardware_flag=args.hardware, allow_lan_flag=args.allow_lan)
            token = config.resolved_token()
            asyncio.run(
                _serve_forever(
                    config,
                    token,
                    hardware_flag=args.hardware,
                    allow_lan_flag=args.allow_lan,
                )
            )
            return 0
        if args.command == "export":
            result = export_lerobot_v3(
                args.episode,
                args.output,
                repo_id=args.repo_id,
                cameras=args.cameras,
                max_image_delta_ms=args.max_image_delta_ms,
                lerobot_source=args.lerobot_source,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            failed = False
            for episode in args.episode:
                failures = verify_manifest(episode)
                if failures:
                    failed = True
                    print(f"FAIL {episode}")
                    for failure in failures:
                        print(f"  {failure}")
                else:
                    print(f"OK   {episode}")
            return 1 if failed else 0
    except (ConfigError, ExportError, RuntimeError, OSError) as error:
        print(f"ERROR: {error}")
        return 2
    return 2


async def _serve_forever(
    config: AppConfig,
    token: str,
    *,
    hardware_flag: bool,
    allow_lan_flag: bool,
) -> None:
    server = TeleopServer(
        config,
        token,
        hardware_flag=hardware_flag,
        allow_lan_flag=allow_lan_flag,
    )
    await server.start()
    try:
        await asyncio.Future()
    finally:
        await server.close()
