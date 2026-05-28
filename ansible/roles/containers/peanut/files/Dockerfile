FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nut \
    nut-client \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY config/ /etc/nut/
RUN chown -R root:nut /etc/nut/ \
    && chmod 640 /etc/nut/*

COPY entrypoint.sh /entrypoint.sh
COPY upssched-cmd /usr/local/bin/upssched-cmd
RUN chmod +x /entrypoint.sh /usr/local/bin/upssched-cmd

ENTRYPOINT ["/entrypoint.sh"]
