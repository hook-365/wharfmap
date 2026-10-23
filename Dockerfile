FROM python:3.14-alpine
RUN apk add --no-cache docker-cli
WORKDIR /app
COPY pyproject.toml README.md ./
COPY wharfmap ./wharfmap
RUN pip install --no-cache-dir .
VOLUME /out
EXPOSE 3008
# Mount the Docker socket (read-only is enough) and optionally your compose tree for config_refs.
ENTRYPOINT ["wharfmap"]
# serve/agent read these as defaults, so `command: serve` or `command: agent` in compose is enough.
ENV WHARFMAP_BIND=0.0.0.0 WHARFMAP_OUT=/out
# Interval comes from [output] every in wharfmap.toml (default 300 s) or --every.
# Keep CMD on its own line: a trailing comment turns the JSON array into shell form.
CMD ["serve"]
