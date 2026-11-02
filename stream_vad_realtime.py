#!/usr/bin/env python3
"""语音输入 daemon 入口（systemd 经 daemon_launch.sh 启动）：解析参数并运行 sensevoice.pipeline 主循环。"""
from sensevoice.cli.args import parse_args
from sensevoice.pipeline import SenseVoicePipeline


def main() -> int:
    args = parse_args()
    pipeline = SenseVoicePipeline(args)
    return pipeline.run()


if __name__ == "__main__":
    raise SystemExit(main())
