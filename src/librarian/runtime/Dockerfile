FROM scratch
ADD rootfs.tar /
USER 65534:65534
WORKDIR /scratch
ENV PATH=/usr/local/bin:/usr/bin:/bin HOME=/scratch HERMES_HOME=/scratch/hermes TMPDIR=/scratch PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/usr/local/bin/python", "-I", "-m", "broker.librarian_agent_client"]
