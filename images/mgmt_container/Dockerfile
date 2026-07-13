FROM alpine:3.20

ENV LANG=C.UTF-8

RUN    apk update \
    && apk add --no-cache \
       docker-cli \
       bash \
       curl \
       jq \
       python3 \
       py3-pip \
    && python3 -m pip install --break-system-packages --no-cache-dir \
       pytest \
       allure-pytest \
       docker \
       pyyaml \
    &&  mkdir /usr/testlibs

COPY  libs/traffic_client.py /usr/testlibs/
COPY  libs/docker_env.py /usr/testlibs/

CMD ["docker", "version"]
