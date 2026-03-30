#!/usr/bin/env python3
"""SenseVoice 语音输入系统 - 主入口

模块化架构：各功能组件在 sensevoice/ 包中独立实现。
本文件仅作为入口点，协调参数解析和管线启动。
"""
from sensevoice.cli.args import parse_args
from sensevoice.pipeline import SenseVoicePipeline


def main() -> int:
    args = parse_args()
    pipeline = SenseVoicePipeline(args)
    return pipeline.run()


if __name__ == "__main__":
    raise SystemExit(main())
