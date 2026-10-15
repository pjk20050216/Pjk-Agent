# 将 IK 中文分词插件固化进镜像，避免每次重建容器都要重装插件。
# 插件版本必须与基础镜像版本一致（8.17.0），否则安装会失败。
FROM docker.elastic.co/elasticsearch/elasticsearch:8.17.0

RUN bin/elasticsearch-plugin install --batch \
    https://get.infini.cloud/elasticsearch/analysis-ik/8.17.0
