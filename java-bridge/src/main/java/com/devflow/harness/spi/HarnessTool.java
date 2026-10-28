package com.devflow.harness.spi;

import java.util.Map;

/**
 * Java 侧工具扩展点（SPI）。
 *
 * <p>任何 Spring Bean 实现该接口后会被自动注册到工具清单，Python Harness
 * 无需改动即可发现并调用。实现类应保证：</p>
 * <ul>
 *   <li>{@link #name()} 全局唯一，建议使用 snake_case；</li>
 *   <li>{@link #parametersSchema()} 返回 JSON Schema（type=object）；</li>
 *   <li>{@link #risk()} 返回 low / medium / high / critical，供 Python 侧审批策略使用；</li>
 *   <li>{@link #action()} 返回审批动作标识，默认 {@code java.tool.invoke}；</li>
 *   <li>{@link #execute(Map)} 不得抛出未包装的运行时异常。</li>
 * </ul>
 */
public interface HarnessTool {

    String name();

    String description();

    Map<String, Object> parametersSchema();

    default String risk() {
        return "medium";
    }

    default String action() {
        return "java.tool.invoke";
    }

    default boolean requiresApproval() {
        return false;
    }

    /**
     * 执行工具。返回结构会原样透传给 Python Agent。
     *
     * @param arguments 调用参数，已由 Python 侧按 schema 过滤
     * @return 可 JSON 序列化的结果
     */
    Map<String, Object> execute(Map<String, Object> arguments) throws Exception;
}
