# Java 工具扩展桥

Python Harness 的 Java 扩展面：用 Spring Boot 暴露工具清单与调用接口，Python 侧自动发现并包装为
`HarnessTool`，与内置工具一样参与权限校验、审批、审计与链路追踪。

## 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/internal/harness/tools` | 返回工具清单（name / description / parameters / risk / action / requires_approval） |
| POST | `/internal/harness/tools/{name}/invoke` | 调用工具，body 为 `{arguments, run_id, user_id}` |

签名（可选但生产必须）：请求头 `X-Harness-Timestamp` + `X-Harness-Signature`，
其中 `signature = HMAC-SHA256(secret, timestamp + "." + body)`，时间窗 ±300 秒。

## 新增一个工具

1. 在 `src/main/java/com/devflow/harness/tools/` 下新建类，实现 `HarnessTool`；
2. 标注 `@Component`；
3. 实现 `name()`、`description()`、`parametersSchema()`、`execute()`；
4. 按需覆盖 `risk()` / `action()` / `requiresApproval()`。

Python 侧会把该工具纳入 `java.tool.invoke` 审批动作（默认高风险），
可在 `config/settings.yaml` 的 `harness.approvals.required_actions` 中调整。

## 启动

```bash
cd java-bridge
mvn spring-boot:run
# 或
mvn clean package && java -jar target/harness-java-bridge-0.1.0.jar
```

## 接入 Python

```bash
HARNESS_JAVA_BRIDGE_ENABLED=true
HARNESS_JAVA_BASE_URL=http://127.0.0.1:8081
HARNESS_JAVA_SHARED_SECRET=change-me
```

或在 `config/settings.yaml`：

```yaml
harness:
  java_bridge:
    enabled: true
    base_url: "http://127.0.0.1:8081"
    shared_secret: "change-me"
```

启动 Python 服务后访问 `GET /api/harness/tools` 应能看到 `java_feature_risk_summary`。
