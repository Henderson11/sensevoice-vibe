"""Text injector factory."""


def create_injector(mode: str, **kwargs):
    """根据模式创建注入器"""
    if mode == "ibus":
        from sensevoice.inject.ibus import IBusInjector
        return IBusInjector()
    else:
        from sensevoice.inject.clipboard import FocusInjector
        return FocusInjector(kwargs.get("script_path", ""))
