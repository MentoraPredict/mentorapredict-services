FROM eclipse-temurin:21-jdk@sha256:8ad118b146aa92da815b576441acd6b42b38236797ad0b6edc8c200b85bb0734 AS jdk

# Keeps only the files that define the build, so editing sources does not change the next stage.
FROM jdk AS build-files
WORKDIR /workspace
COPY . .
RUN mkdir /build-files \
    && cp -r --parents gradlew gradle build-logic /build-files/ \
    && find . \( -name build.gradle.kts -o -name settings.gradle.kts -o -name gradle.properties \) \
        -exec cp --parents {} /build-files/ \;

FROM jdk AS build
WORKDIR /workspace
# Cached layer: Gradle, build-logic and dependencies. It is rebuilt only when the files above change.
COPY --from=build-files /build-files/ ./
RUN chmod +x gradlew && ./gradlew :services:identity-service:downloadDependencies --no-daemon
COPY . .
RUN chmod +x gradlew && ./gradlew :services:identity-service:bootJar --no-daemon

FROM eclipse-temurin:21-jre@sha256:d77e090ae1f6970c047b443c6f0a6c1358b0426d44432bb5a7753408cd43de0d
WORKDIR /app
COPY --from=build /workspace/services/identity-service/build/libs/*.jar /app/identity-service.jar
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/identity-service.jar"]
