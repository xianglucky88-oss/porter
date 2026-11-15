"""Porter —— 内部表单录入的 RPA 运行时。

模块划分：
    config          环境变量与默认值
    models          YAML 流程定义与运行记录的数据结构
    pageview        页面结构快照（两种驱动共用同一解析器）
    resolver        多候选定位：确定性回退 + 歧义拒绝
    failures        失败语义分类（页面加载/结构变化/字段缺失/业务校验…）
    evidence        执行证据采集与脱敏
    field_registry  业务字段的已知可见标签
    selectors       旧版单候选选择器表（兼容层，勿用于新流程）
    executor        顺序执行引擎与驱动实现
    store           PostgreSQL 运行记录
    api             FastAPI 服务
"""

__version__ = "0.10.0"
