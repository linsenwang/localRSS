/*
 * 专用微信库解密器：固定路径、不接受参数，只 exec decrypt_db.py。
 *
 * 由 launchd 启动，TCC 的责任进程就是它自己（不是全局 node）。
 * 安装后整个目录 root:wheel，执行的 Python 脚本和 sqlcipher 也都是 root-owned，
 * 所以可以只给这一个二进制完全磁盘访问权限（FDA），不扩大 node 的权限面。
 */

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef PYTHON_PATH
#error "PYTHON_PATH is required"
#endif
#ifndef SCRIPT_PATH
#error "SCRIPT_PATH is required"
#endif
#ifndef KEYS_PATH
#error "KEYS_PATH is required"
#endif
#ifndef OUT_DIR
#error "OUT_DIR is required"
#endif
#ifndef TIMEOUT_S
#define TIMEOUT_S 120
#endif

static int wait_with_timeout(pid_t pid, int seconds) {
    time_t deadline = time(NULL) + seconds;
    int status = 0;

    for (;;) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid) {
            return status;
        }
        if (r < 0) {
            perror("waitpid");
            return -1;
        }
        if (time(NULL) >= deadline) {
            fprintf(stderr, "timeout after %d s\n", seconds);
            kill(-pid, SIGTERM);
            sleep(2);
            kill(-pid, SIGKILL);
            waitpid(pid, &status, 0);
            return -1;
        }
        sleep(1);
    }
}

int main(int argc, char **argv) {
    (void)argv;
    if (argc != 1) {
        fprintf(stderr, "wechat-refresh takes no arguments\n");
        return 2;
    }

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }
    if (pid == 0) {
        setpgid(0, 0);
        execl(PYTHON_PATH, PYTHON_PATH, SCRIPT_PATH,
              "--keys", KEYS_PATH,
              "-o", OUT_DIR,
              "--skip-unchanged", (char *)NULL);
        perror("execl");
        _exit(127);
    }

    setpgid(pid, pid);
    int status = wait_with_timeout(pid, TIMEOUT_S);
    if (status < 0) {
        return 124;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
