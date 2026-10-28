package com.devflow.harness.registry;

import com.devflow.harness.spi.HarnessTool;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/** 收集所有 HarnessTool Bean，生成清单并提供按名查找。 */
@Component
public class HarnessToolRegistry {

    private final Map<String, HarnessTool> tools = new LinkedHashMap<>();

    public HarnessToolRegistry(List<HarnessTool> discovered) {
        for (HarnessTool tool : discovered) {
            HarnessTool previous = tools.put(tool.name(), tool);
            if (previous != null) {
                throw new IllegalStateException("duplicate harness tool name: " + tool.name());
            }
        }
    }

    public List<HarnessTool> list() {
        return List.copyOf(tools.values());
    }

    public Optional<HarnessTool> find(String name) {
        return Optional.ofNullable(tools.get(name));
    }

    public Map<String, Object> manifestEntry(HarnessTool tool) {
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("name", tool.name());
        entry.put("description", tool.description());
        entry.put("parameters", tool.parametersSchema());
        entry.put("risk", tool.risk());
        entry.put("action", tool.action());
        entry.put("requires_approval", tool.requiresApproval());
        entry.put("source", "java");
        return entry;
    }
}
