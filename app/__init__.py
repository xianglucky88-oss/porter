"""Porter —— 内部表单录入的 RPA 运行时。

模块划分：
    config     环境变量与默认值
    models     YAML 流程定义、运行记录与批次生命周期的数据结构
    selectors  选择器表
    executor   顺序执行引擎（单发与批次共享步骤循环）
    batch      批次执行器：最终性语义、证据确认、人工处置
    store      PostgreSQL 运行记录与批次状态（版本化迁移）
    api        FastAPI 服务
"""

__version__ = "0.10.0"
