# Java

Version: JDK 21

Java supports two running modes: `java` and `junit`.

## Java

In `java` mode, the compilation command is `javac -cp .:javatuples-1.2.jar:<jars> Main.java`, and the execution command is `java -cp .:javatuples-1.2.jar:<jars> -ea Main`.

Here, `jars` refers to all files ending with `.jar` in the files passed as parameters.

Please ensure that the Java class name passed in is Main.

## JUnit

In `junit` mode, the compilation command is

```bash
javac -cp .:junit-platform-console-standalone-1.8.2.jar:junit-jupiter-api-5.11.0-javadoc.jar:<jars> *.java
```

and the execution command is

```bash
java -jar ./junit-platform-console-standalone-1.8.2.jar --class-path .:junit-platform-console-standalone-1.8.2.jar:junit-jupiter-api-5.11.0-javadoc.jar:<jars> --scan-class-path
```

The sandbox automatically detects the class name in the code passed as the code parameter and places it in the corresponding `.java` file. If no class name is found, it will still be placed in `Main.java`.
