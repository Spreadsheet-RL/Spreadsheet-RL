package main

import (
	"fmt"

	_ "github.com/go-openapi/inflect"
	// testify
	_ "github.com/stretchr/testify/assert"
	// uber libs
	_ "go.uber.org/atomic"
	_ "go.uber.org/automaxprocs"
	_ "go.uber.org/goleak"
	_ "go.uber.org/zap"

	// orm
	_ "gorm.io/gorm"
	// opentelemetry
	_ "go.opentelemetry.io/otel"
	_ "go.opentelemetry.io/otel/exporters/stdout/stdoutmetric"
	_ "go.opentelemetry.io/otel/metric"
	_ "go.opentelemetry.io/otel/sdk"
	_ "go.opentelemetry.io/otel/sdk/metric"
	_ "go.opentelemetry.io/otel/trace"

	// opentracing
	_ "github.com/opentracing/opentracing-go"
	// yaml
	_ "gopkg.in/yaml.v2"
	_ "gopkg.in/yaml.v3"

	// golang x
	_ "golang.org/x/oauth2"
	_ "golang.org/x/text"

	// viper
	_ "github.com/spf13/cobra"
	_ "github.com/spf13/pflag"
	_ "github.com/spf13/viper"

	// logger
	_ "github.com/sirupsen/logrus"

	// other libs
	_ "github.com/go-enry/go-enry/v2"
)

func main() {
	fmt.Println("123")
}
