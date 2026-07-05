# egress-proxy: sole egress path for agent-runner containers (ADR-025).
# Build context: services/egress-proxy
FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -o /out/egress-proxy ./cmd/egress-proxy

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/egress-proxy /egress-proxy
EXPOSE 18082
ENTRYPOINT ["/egress-proxy"]
