package com.devflow.harness.spi;

import java.util.Map;

/** 统一返回结构，与 Python 侧约定一致。 */
public record ToolResponse(boolean success, Object data, String error) {

    public static ToolResponse ok(Object data) {
        return new ToolResponse(true, data, null);
    }

    public static ToolResponse error(String message) {
        return new ToolResponse(false, Map.of(), message);
    }
}
