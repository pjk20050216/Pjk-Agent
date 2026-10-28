package com.devflow.harness.tools;

import com.devflow.harness.spi.HarnessTool;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 示例 Java 工具：查询规则关联特征的风险摘要。
 *
 * <p>真实项目中可替换为调用内部特征平台 / 风控决策引擎。此示例用于验证
 * Python Harness 的 Java 工具发现与调用链路。</p>
 */
@Component
public class FeatureRiskSummaryTool implements HarnessTool {

    @Override
    public String name() {
        return "java_feature_risk_summary";
    }

    @Override
    public String description() {
        return "（Java 扩展）汇总规则关联特征的覆盖率、空值率与 PSI，返回风险摘要。";
    }

    @Override
    public Map<String, Object> parametersSchema() {
        Map<String, Object> properties = new LinkedHashMap<>();
        properties.put("rule_id", Map.of("type", "string", "description", "规则 ID"));
        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", properties);
        schema.put("required", new String[]{"rule_id"});
        schema.put("additionalProperties", false);
        return schema;
    }

    @Override
    public String risk() {
        return "low";
    }

    @Override
    public String action() {
        return "java.tool.invoke";
    }

    @Override
    public Map<String, Object> execute(Map<String, Object> arguments) {
        String ruleId = String.valueOf(arguments.getOrDefault("rule_id", "R_RISK_001"));
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("rule_id", ruleId);
        result.put("source", "java-bridge");
        result.put("features", new Object[]{
                Map.of("name", "device_risk_score", "coverage", 0.988, "null_rate", 0.012, "psi", 0.08),
                Map.of("name", "login_fail_count_1h", "coverage", 0.998, "null_rate", 0.002, "psi", 0.12),
                Map.of("name", "same_device_user_count_7d", "coverage", 0.994, "null_rate", 0.006, "psi", 0.44)
        });
        result.put("risk_summary", "same_device_user_count_7d 存在显著漂移（PSI=0.44），建议核对上游口径");
        return result;
    }
}
