"""外部系统集成：Kafka 审计、Java 工具桥。"""
from harness.integrations.java_bridge import JavaToolBridge, java_bridge
from harness.integrations.kafka import KafkaAuditSink, sink

__all__ = ["JavaToolBridge", "java_bridge", "KafkaAuditSink", "sink"]
