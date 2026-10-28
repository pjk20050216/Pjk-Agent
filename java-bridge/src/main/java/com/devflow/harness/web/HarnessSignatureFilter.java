package com.devflow.harness.web;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;

/**
 * 内部接口签名校验：HMAC-SHA256(timestamp + "." + body)。
 *
 * <p>密钥为空时跳过校验（仅建议本地开发使用）。生产环境必须配置
 * {@code HARNESS_JAVA_SHARED_SECRET}，并与 Python 侧保持一致。</p>
 */
@Component
public class HarnessSignatureFilter extends OncePerRequestFilter {

    private static final long MAX_SKEW_SECONDS = 300;

    private final String sharedSecret;

    public HarnessSignatureFilter(
            @Value("${harness.bridge.shared-secret:}") String sharedSecret) {
        this.sharedSecret = sharedSecret == null ? "" : sharedSecret.trim();
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain) throws ServletException, IOException {

        if (sharedSecret.isEmpty() || !request.getRequestURI().startsWith("/internal/harness/")) {
            filterChain.doFilter(request, response);
            return;
        }

        byte[] body = request.getInputStream().readAllBytes();
        String timestamp = request.getHeader("X-Harness-Timestamp");
        String signature = request.getHeader("X-Harness-Signature");
        if (!verify(timestamp, signature, body)) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            response.getWriter().write("{\"success\":false,\"error\":\"invalid signature\"}");
            return;
        }

        filterChain.doFilter(new CachedBodyRequest(request, body), response);
    }

    private boolean verify(String timestamp, String signature, byte[] body) {
        if (timestamp == null || signature == null) {
            return false;
        }
        try {
            long ts = Long.parseLong(timestamp);
            if (Math.abs(Instant.now().getEpochSecond() - ts) > MAX_SKEW_SECONDS) {
                return false;
            }
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(sharedSecret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            mac.update(timestamp.getBytes(StandardCharsets.UTF_8));
            mac.update((byte) '.');
            byte[] expected = mac.doFinal(body);
            return MessageDigest.isEqual(expected, hexToBytes(signature));
        } catch (Exception ex) {
            return false;
        }
    }

    private static byte[] hexToBytes(String hex) {
        int length = hex.length();
        byte[] out = new byte[length / 2];
        for (int i = 0; i < length; i += 2) {
            out[i / 2] = (byte) Integer.parseInt(hex.substring(i, i + 2), 16);
        }
        return out;
    }

    /** 让后续 Controller 仍能读取 body。 */
    private static final class CachedBodyRequest extends HttpServletRequestWrapper {
        private final byte[] body;

        private CachedBodyRequest(HttpServletRequest request, byte[] body) {
            super(request);
            this.body = body;
        }

        @Override
        public ServletInputStream getInputStream() {
            ByteArrayInputStream stream = new ByteArrayInputStream(body);
            return new ServletInputStream() {
                @Override
                public boolean isFinished() {
                    return stream.available() == 0;
                }

                @Override
                public boolean isReady() {
                    return true;
                }

                @Override
                public void setReadListener(ReadListener listener) {
                    // 同步读取，无需实现
                }

                @Override
                public int read() {
                    return stream.read();
                }
            };
        }

        @Override
        public BufferedReader getReader() {
            return new BufferedReader(
                    new InputStreamReader(getInputStream(), StandardCharsets.UTF_8));
        }
    }
}
