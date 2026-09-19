FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/orc-telemetry .

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/orc-telemetry /orc-telemetry
EXPOSE 8080
ENTRYPOINT ["/orc-telemetry"]
