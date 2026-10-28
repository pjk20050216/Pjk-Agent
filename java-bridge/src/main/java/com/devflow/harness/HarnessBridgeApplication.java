package com.devflow.harness;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * Java 工具扩展桥启动类。
 *
 * <p>Python Harness 通过 {@code GET /internal/harness/tools} 拉取工具清单，
 * 通过 {@code POST /internal/harness/tools/{name}/invoke} 调用具体工具。</p>
 */
@SpringBootApplication
public class HarnessBridgeApplication {

    public static void main(String[] args) {
        SpringApplication.run(HarnessBridgeApplication.class, args);
    }
}
