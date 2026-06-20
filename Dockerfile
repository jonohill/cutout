FROM debian:bookworm-slim AS ffmpeg-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf automake build-essential ca-certificates curl libtool \
        libopus-dev nasm pkg-config yasm zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ARG FDK_AAC_VERSION=2.0.3
RUN curl -fsSL "https://github.com/mstorsjo/fdk-aac/archive/refs/tags/v${FDK_AAC_VERSION}.tar.gz" \
        | tar xz \
    && cd "fdk-aac-${FDK_AAC_VERSION}" \
    && ./autogen.sh \
    && ./configure --prefix=/usr/local --disable-shared --enable-static \
    && make -j"$(nproc)" \
    && make install

ARG FFMPEG_VERSION=7.1
RUN curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
        | tar xJ \
    && cd "ffmpeg-${FFMPEG_VERSION}" \
    && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure \
        --prefix=/usr/local \
        --enable-nonfree \
        --enable-libfdk-aac \
        --enable-libopus \
        --extra-ldflags="-static-libstdc++ -static-libgcc" \
        --disable-debug \
        --disable-doc \
        --disable-ffplay \
    && make -j"$(nproc)" \
    && make install

# --- runtime stage ----------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    WORK_DIR=/work

# Only libopus is needed at runtime — fdk-aac and the C++ runtime are linked
# into the ffmpeg binary above. ffmpeg/ffprobe are the binaries we just built.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg-build /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# In-flight media (downloads, transcripts, cut audio) lands here. Declared as a
# volume so the host can mount it (`-v /host/path:/work`) to inspect work or let
# an interrupted job resume across a container restart.
RUN mkdir -p /work
VOLUME ["/work"]

EXPOSE 8080

CMD ["cutout"]
