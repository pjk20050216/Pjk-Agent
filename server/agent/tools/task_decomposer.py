"""
 将技术方案拆解为可执行的开发任务列表，包含优先级、预估工时和依赖关系。
 """
from server.agent.base_tool import BaseTool
from server.agent.tool_result import ToolResult
from server.services.llm_service import llm_service
from server.utils.logger import logger


class TaskDecomposer(BaseTool):
    name = "task_decomposer"
    description = "根据技术方案将开发工作拆解为具体的可执行任务，每个任务包含标题、描述、优先级、预估工时和前置依赖。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "tech_plan": {
                "type": "string",
                "description": "TechPlanGenerator 工具输出的技术方案（Markdown 格式）",
            },
            "team_size": {
                "type": "integer",
                "description": "团队人数，用于评估并行度和排期（可选，默认 3）",
            },
        },
        "required": ["tech_plan"],
    }

    async def execute(self, tech_plan: str, team_size: int = 3) -> ToolResult:
        try:
            prompt = f"""你是一个资深的项目经理/技术主管。请根据以下技术方案，将开发工作拆解为具体的可执行任务。

## 技术方案
{tech_plan}

⚠️ 重要规则：
- 任务拆解必须覆盖技术方案中的所有模块和接口
- 只拆解技术方案中明确提到的内容，不要添加方案中未涉及的额外任务
- 如果方案中某个模块信息不足，标注「需进一步明确」而不是自行补充

## 拆解要求

请将工作拆解为 10-20 个具体的开发任务，每个任务包含以下字段，最终以 JSON 数组格式输出：

```json
[
  {{
    "id": "T001",
    "title": "任务标题（简洁明确）",
    "description": "详细描述，说明要做什么、涉及哪些文件/模块",
    "priority": "P0/P1/P2/P3",
    "estimated_hours": 8,
    "dependencies": ["T000"],
    "assignee_role": "后端/前端/全栈/DBA",
    "acceptance_criteria": "验收标准"
  }}
]
```

要求：
- 任务粒度适中，每个任务预估 4-24 小时
- 优先级 P0=阻塞性/必须最先做, P1=核心功能, P2=重要但可稍后, P3=锦上添花
- 依赖关系要合理，体现真实的开发顺序
- 覆盖数据库设计、后端开发、前端开发、测试等全流程
- 考虑团队规模为 {team_size} 人，标注哪些任务可以并行

请直接输出 JSON 数组，不要包含其他说明文字。"""

            messages = [
                {"role": "system", "content": "你是一个资深项目经理，擅长将技术方案拆解为可执行的开发任务。只基于给定的技术方案拆解，不自行添加任务。始终输出规范的 JSON。"},
                {"role": "user", "content": prompt},
            ]
            response = await llm_service.chat(messages, temperature=0.3, max_tokens=4096)

            # 尝试提取 JSON 数组，至于为什么有三种情况，因为有些回复的 JSON 可能格式不行
            import json
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()
            elif "[" in response:
                start = response.index("[")
                end = response.rindex("]") + 1
                json_str = response[start:end]

            tasks = json.loads(json_str)

            return ToolResult(
                success=True,
                data={
                    "tasks": tasks,
                    "count": len(tasks),
                    "team_size": team_size,
                },
            )
        except json.JSONDecodeError as e:
            logger.warning(f"TaskDecomposer JSON parse error: {e}")
            return ToolResult(
                success=True,
                data={
                    "tasks_raw": response,
                    "count": 0,
                    "note": "JSON 解析失败，返回原始文本",
                },
            )
        except Exception as e:
            logger.error(f"TaskDecomposer failed: {e}")
            return ToolResult(success=False, error=str(e))
