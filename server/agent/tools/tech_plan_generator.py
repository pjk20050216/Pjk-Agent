"""TechPlanGenerator —— 根据需求分析结果生成技术方案，包含架构设计、技术栈、模块划分等。"""
from server.agent.base_tool import BaseTool
from server.agent.tool_result import ToolResult
from server.services.llm_service import llm_service
from server.utils.logger import logger


class TechPlanGenerator(BaseTool):
    name = "tech_plan_generator"
    description = "根据产品需求文档的分析结果，生成完整的技术方案，包括系统架构、技术栈选型、模块划分、接口设计等。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "requirements_analysis": {
                "type": "string",
                "description": "DocumentReader 工具输出的需求分析结果（Markdown 格式）",
            },
            "constraints": {
                "type": "string",
                "description": "技术约束或偏好，如'使用 Python + FastAPI'、'数据库用 PostgreSQL'等（可选）",
            },
        },
        "required": ["requirements_analysis"],
    }

    async def execute(self, requirements_analysis: str, constraints: str = "") -> ToolResult:
        try:
            constraint_text = f"\n\n技术约束：{constraints}" if constraints else ""

            prompt = f"""你是一个资深系统架构师。请根据以下需求分析结果，生成一份完整的技术方案。

## 需求分析
{requirements_analysis}{constraint_text}

⚠️ 重要规则：
- 技术方案必须严格覆盖需求分析中提到的所有功能模块和业务实体
- 不得添加需求分析中未提及的功能或模块
- 如果需求分析中某些方面信息不足，请标注「需求信息不足，建议补充」

请生成以下内容：
# ... (content continues below) ...

### 1. 系统架构设计
- 整体架构风格（单体/微服务/Serverless 等）及理由
- 系统分层架构图（用 ASCII Art 或文字描述）
- 各层的职责说明

### 2. 技术栈选型
- 后端框架及理由
- 数据库选型及理由
- 中间件/基础设施选型
- 前端技术栈（如适用）

### 3. 模块划分
- 核心模块列表及职责
- 模块间依赖关系
- 每个模块的关键类和接口概要

### 4. API 接口设计
- 列出主要的 RESTful API 端点（方法、路径、请求/响应概要）
- WebSocket/SSE 端点（如需要）

### 5. 数据流设计
- 关键业务流程的数据流向
- 异步处理和消息队列设计（如需要）

请用结构化的 Markdown 输出，尽量具体、可落地。"""

            messages = [
                {"role": "system", "content": "你是一个资深系统架构师，擅长从需求出发设计可落地的技术方案。你只基于给定的需求分析输出方案，不添加需求中未提及的功能。"},
                {"role": "user", "content": prompt},
            ]
            plan = await llm_service.chat(messages, temperature=0.4, max_tokens=8192)

            return ToolResult(
                success=True,
                data={"tech_plan": plan},
            )
        except Exception as e:
            logger.error(f"TechPlanGenerator failed: {e}")
            return ToolResult(success=False, error=str(e))
