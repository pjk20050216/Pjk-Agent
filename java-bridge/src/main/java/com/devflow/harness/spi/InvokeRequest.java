package com.devflow.harness.spi;

import java.util.Map;

/** Python 侧调用入参。 */
public record InvokeRequest(
        Map<String, Object> arguments,
        String runId,
        Long userId
) {
    public InvokeRequest {
        if (arguments == null) {
            arguments = Map.of();
        }
    }
}
