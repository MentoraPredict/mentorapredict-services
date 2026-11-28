// Convention for every microservice under services/*.
// Applies Java 21, Spring Boot (BOM and executable jar), the test stack and the
// quality tools. Dependencies between services are rejected (rule H4).

import org.springframework.boot.gradle.plugin.SpringBootPlugin

plugins {
    id("mentora.calidad")
    id("org.springframework.boot")
}

val libs = the<VersionCatalogsExtension>().named("libs")

dependencies {
    // Spring Boot manages the versions of its own starters and libraries.
    implementation(platform(SpringBootPlugin.BOM_COORDINATES))
    testImplementation(platform(libs.findLibrary("testcontainers-bom").get()))

    testImplementation("org.springframework.boot:spring-boot-starter-test")
    testImplementation(libs.findLibrary("archunit").get())
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

// Downloads what bootJar needs without compiling the service. The Dockerfile runs it before
// copying the sources, so Docker keeps the dependencies in a cached layer.
tasks.register("downloadDependencies") {
    description = "Downloads the dependencies needed to build the executable jar."
    val classpaths =
        listOf("annotationProcessor", "compileClasspath", "runtimeClasspath").map { name ->
            configurations[name].incoming.artifactView {
                // Project dependencies are excluded: their jars would need the sources.
                componentFilter { it is ModuleComponentIdentifier }
            }.files
        }
    doLast { classpaths.forEach { it.files } }
}

// Integration tests live in src/integrationTest and run with ./gradlew integrationTest.
// They talk to real PostgreSQL and Kafka through Testcontainers (DOC-ARQ-13 section 6), so they
// need Docker and are not part of `check`: a clone with only JDK 21 still builds and tests.
val integrationTest: SourceSet =
    sourceSets.create("integrationTest") {
        compileClasspath += sourceSets["main"].output
        runtimeClasspath += sourceSets["main"].output
    }

configurations["integrationTestImplementation"].extendsFrom(
    configurations["implementation"], configurations["testImplementation"])
configurations["integrationTestRuntimeOnly"].extendsFrom(
    configurations["runtimeOnly"], configurations["testRuntimeOnly"])

dependencies {
    // Connects Spring Boot to the containers (@ServiceConnection) and starts them from the tests.
    "integrationTestImplementation"("org.springframework.boot:spring-boot-testcontainers")
    "integrationTestImplementation"("org.testcontainers:testcontainers-junit-jupiter")
    "integrationTestImplementation"("org.testcontainers:testcontainers-postgresql")
    "integrationTestImplementation"("org.testcontainers:testcontainers-kafka")
}

tasks.register<Test>("integrationTest") {
    description = "Runs the integration tests against real containers (needs Docker)."
    group = "verification"
    testClassesDirs = integrationTest.output.classesDirs
    classpath = integrationTest.runtimeClasspath
    shouldRunAfter(tasks.named("test"))
}
