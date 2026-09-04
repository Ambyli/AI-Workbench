# Standalone Hive Metastore for Trino's Iceberg connector.
#
# apache/hive:4.0.0 ships the standalone metastore binary. Two extras we
# bake in:
#
#   1. Postgres JDBC driver — HMS reads/writes its own catalog tables in
#      hive-metastore-db via JDBC. Not bundled in the base image.
#   2. hadoop-aws + aws-java-sdk-bundle — HMS needs the s3a filesystem
#      client so it can validate that Iceberg manifest paths on MinIO
#      exist at CREATE TABLE time. Without these jars, `CREATE TABLE …
#      WITH (location='s3://warehouse/…')` fails with
#      java.io.IOException: No FileSystem for scheme "s3".
#
# Runtime config lives in metastore-site.xml (mounted alongside).

FROM apache/hive:4.0.0

USER root

ENV POSTGRES_JDBC_VERSION=42.7.4 \
    HADOOP_AWS_VERSION=3.3.6 \
    AWS_SDK_BUNDLE_VERSION=1.12.767

# apache/hive:4.0.0 (eclipse-temurin base) doesn't ship curl; the JDBC /
# hadoop-aws / aws-sdk downloads below need it. Install once, drop the
# apt cache to keep the layer small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Postgres JDBC driver
RUN curl -fSL \
      "https://jdbc.postgresql.org/download/postgresql-${POSTGRES_JDBC_VERSION}.jar" \
      -o /opt/hive/lib/postgresql-${POSTGRES_JDBC_VERSION}.jar

# S3A filesystem client + AWS SDK
RUN curl -fSL \
      "https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar" \
      -o /opt/hive/lib/hadoop-aws-${HADOOP_AWS_VERSION}.jar \
 && curl -fSL \
      "https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK_BUNDLE_VERSION}/aws-java-sdk-bundle-${AWS_SDK_BUNDLE_VERSION}.jar" \
      -o /opt/hive/lib/aws-java-sdk-bundle-${AWS_SDK_BUNDLE_VERSION}.jar

COPY metastore-site.xml /opt/hive/conf/metastore-site.xml

# Base image's entrypoint runs schematool + starts HMS. SERVICE_NAME is
# what the standalone metastore image branches on.
ENV SERVICE_NAME=metastore \
    DB_DRIVER=postgres

USER hive
