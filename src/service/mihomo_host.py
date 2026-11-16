"""Linux parent-death binding for the service's standalone Mihomo executable."""
import os
import sys

from src.service.worker_process import bind_linux_parent


if __name__ == '__main__':
    bind_linux_parent(int(sys.argv[1]))
    os.execv(sys.argv[2], sys.argv[2:])
