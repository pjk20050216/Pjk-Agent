package com.devflow.harness.web;

import com.devflow.harness.registry.HarnessToolRegistry;
import com.devflow.harness.spi.HarnessTool;
import com.devflow.harness.spi.InvokeRequest;
import com.devflow.harness.spi.ToolResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Java 工具清单与调用入口。
 *
 * <p>契约与 Python 侧 {@code harness/integrations/java_bridge.py} 保持一致：</p>
 * <pre>
 * GET  /internal/harness/tools
 * POST /internal/harness/tools/{name}/invoke
 * </pre>
 */
@RestController
@RequestMapping("/internal/harness/tools")
public class HarnessToolController {

    private static final Logger log = LoggerFactory.getLogger(HarnessToolController.class);

    private final HarnessToolRegistry registry;

    public HarnessToolController(HarnessToolRegistry registry) {
        this.registry = registry;
    }

    @GetMapping
    public Map<String, Object> manifest() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("tools", registry.list().stream().map(registry::manifestEntry).toList());
        body.put("count", registry.list().size());
        return body;
    }

    @PostMapping("/{name}/invoke")
    public ToolResponse invoke(@PathVariable String name, @RequestBody InvokeRequest request) {
        HarnessTool tool = registry.find(name).orElse(null);
        if (tool == null) {
            return ToolResponse.error("unknown tool: " + name);
        }
        try {
            Map<String, Object> result = tool.execute(request.arguments());
            return ToolResponse.ok(result);
        } catch (Exception ex) {
            log.warn("harness tool {} failed", name, ex);
            return ToolResponse.error(ex.getClass().getSimpleName() + ": " + ex.getMessage());
        }
    }
}
