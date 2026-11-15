"""Porter —— 内部表单录入的 RPA 运行时。

模块划分：
    config      环境变量与默认值
    models      YAML 流程定义与运行记录的数据结构
    governance  流程定义的受治理生命周期（不可变版本 / 静态审查 / 审计）
    selectors   选择器表
    executor    顺序执行引擎（含 Http / Browser 两种驱动）
    store       PostgreSQL 运行记录
    api         FastAPI 服务

执行入口只接受 `governance` 中已发布的版本；运行记录绑定
`(flow, version, content_hash)`，重试与恢复沿用启动时确认的版本。
"""

__version__ = "0.10.0"
