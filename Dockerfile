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
CMD ["serve", "--bind", "0.0.0.0", "--port", "3008", "-o", "/out"]   # interval comes from [output] every in wharfmap.toml (default 300 s) or --every
