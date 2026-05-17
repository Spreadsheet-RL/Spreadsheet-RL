# Java

版本： JDK 21

Java 支持两种运行模式： `java` 和 `junit` 。

## Java

`java` 模式下的编译指令为 `javac -cp .:javatuples-1.2.jar:<jars> Main.java` ，执行指令为 `java -cp .:javatuples-1.2.jar:<jars> -ea Main` 。

其中 `jars` 为 `files` 参数传入的文件中，所有以 `.jar` 结尾的文件。

这时，请确保传入的 Java 类名为 Main 。

## JUnit

`junit` 模式下的编译指令为 

```bash
javac -cp .:junit-platform-console-standalone-1.8.2.jar:junit-jupiter-api-5.11.0-javadoc.jar:<jars> *.java
```

，执行指令为 

```bash
java -jar ./junit-platform-console-standalone-1.8.2.jar --class-path .:junit-platform-console-standalone-1.8.2.jar:junit-jupiter-api-5.11.0-javadoc.jar:<jars> --scan-class-path
```

。 沙盒会自动检测传入 code 参数中代码中的类名，并将其放在对应的 `.java` 文件中。 如果没有找到类名，仍然放在 `Main.java` 中。
