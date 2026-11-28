FROM debian:bookworm-slim AS ffmpeg-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake curl \
        libopus-dev nasm pkg-config zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# The xhe-aac branch builds both libfdk_aac and libfdk_xhe_aac. Pinned to
# commits because neither project has a release with the xHE-AAC encoder yet.
ARG FDK_AAC_REF=e309c3faf439cf9adb9a9d60254b77e8961da16f
RUN mkdir fdk-aac \
    && curl -fsSL "https://github.com/mstorsjo/fdk-aac/archive/${FDK_AAC_REF}.tar.gz" \
        | tar xz --strip-components=1 -C fdk-aac \
    && cmake -S fdk-aac -B fdk-aac/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_SHARED_LIBS=OFF \
    && cmake --build fdk-aac/build -j"$(nproc)" \
    && cmake --install fdk-aac/build

ARG FFMPEG_REF=73de9781b6319e0051249b73e7d9b4c09451edf2
RUN mkdir ffmpeg \
    && curl -fsSL "https://github.com/FFmpeg/FFmpeg/archive/${FFMPEG_REF}.tar.gz" \
        | tar xz --strip-components=1 -C ffmpeg \
    && cd ffmpeg \
    && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure \
        --prefix=/usr/local \
        --pkg-config-flags=--static \
        --enable-nonfree \
        --enable-libfdk-aac \
        --enable-libfdk-xhe-aac \
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
