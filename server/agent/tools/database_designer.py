
from server.agent.base_tool import BaseTool
from server.agent.tool_result import ToolResult
from server.services.llm_service import llm_service
from server.utils.logger import logger


# 根据技术方案和任务列表生成数据库设计，包含 ER 图描述、DDL 语句、索引设计等。
class DatabaseDesigner(BaseTool):
    # 下面这段代码是用于定义这个工具是干什么的
    name = "database_designer"
    description = "根据技术方案和业务实体生成数据库设计初稿，包括 ER 图描述、建表 DDL 语句、索引设计、关键查询示例。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "requirements_analysis": {
                "type": "string",
                "description": "DocumentReader 输出的需求分析结果（含业务实体）",
            },
            "tech_plan": {
                "type": "string",
                "description": "TechPlanGenerator 输出的技术方案（可选，用于了解技术栈和架构）",
            },
            "db_type": {
                "type": "string",
                "description": "目标数据库类型，如 MySQL、PostgreSQL、SQLite（默认 PostgreSQL）",
            },
        },
        "required": ["requirements_analysis"],
    }

    # 下面代码是工具的具体工作内容
    async def execute(
        # 其中有三个输入参数：req.._an、tech_plan、db_type
        self, requirements_analysis: str, tech_plan: str = "", db_type: str = "PostgreSQL"
    ) -> ToolResult:
        try:
            tech_context = f"\n\n## 技术方案参考\n{tech_plan}" if tech_plan else ""

            prompt = f"""你是一个资深 DBA/数据库架构师。请根据以下需求分析和技术方案，生成一份完整的数据库设计初稿。

## 需求分析（含业务实体）
{requirements_analysis}{tech_context}

⚠️ 重要规则：
- 只设计需求分析中明确列出的业务实体对应的数据表
- 不得添加需求中未提及的实体或表
- 字段设计必须基于需求中的属性描述，不自创无关字段
- 如果实体属性不完整，标注「属性信息不足」而不是编造

## 设计要求

目标数据库：{db_type}

请输出以下内容：

### 1. ER 图描述
用文字描述实体关系图，包括：
- 每个实体的名称、含义
- 实体之间的关系（1:1、1:N、N:M）
- 关键外键说明

### 2. 建表 DDL
为每个实体生成完整的 CREATE TABLE 语句（{db_type} 语法），包含：
- 主键（推荐使用 BIGSERIAL 或 UUID）
- 所有必要字段及类型
- NOT NULL 约束
- DEFAULT 值
- 外键约束（ON DELETE 行为）
- 注释（COMMENT ON）

### 3. 索引设计
- 主键索引（自动）
- 外键索引
- 业务查询索引（列出 CREATE INDEX 语句和使用场景）
- 唯一约束索引

### 4. 关键查询示例
提供 3-5 个核心业务场景的 SELECT 查询示例（含 JOIN）。

### 5. 数据迁移策略（简要）
- 初始数据如何导入
- 后续 schema 变更策略

请输出完整的 SQL DDL 和结构化文档。"""

            messages = [
                {"role": "system", "content": f"你是一个资深 DBA，精通 {db_type} 数据库设计和 SQL 编写。你只基于给定的需求分析设计数据库，不自行添加业务实体。"},
                {"role": "user", "content": prompt},
            ]
            design = await llm_service.chat(messages, temperature=0.3, max_tokens=8192)

            return ToolResult(
                success=True,
                data={
                    "database_design": design,
                    "db_type": db_type,
                },
            )
        except Exception as e:
            logger.error(f"DatabaseDesigner failed: {e}")
            return ToolResult(success=False, error=str(e))
