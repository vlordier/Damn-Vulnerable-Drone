/*
 * Small parent-owned process-group supervisor for research runners.
 *
 * The managed command remains our direct child and is observed with
 * waitid(WNOWAIT). Its PID therefore cannot be recycled between the exit
 * check and a process-group signal. Control is through a private FIFO named
 * by an owner-only identity file; callers never supply a PID to signal.
 */
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested = 0;

static void on_signal(int signo) { stop_requested = signo; }

static int install_parent_signal_handlers(void) {
  struct sigaction action = {0};
  action.sa_handler = on_signal;
  sigemptyset(&action.sa_mask);
  return sigaction(SIGTERM, &action, NULL) < 0 ||
         sigaction(SIGINT, &action, NULL) < 0 ||
         sigaction(SIGHUP, &action, NULL) < 0;
}

static int restore_default_signal_handlers(void) {
  struct sigaction action = {0};
  action.sa_handler = SIG_DFL;
  sigemptyset(&action.sa_mask);
  return sigaction(SIGTERM, &action, NULL) < 0 ||
         sigaction(SIGINT, &action, NULL) < 0 ||
         sigaction(SIGHUP, &action, NULL) < 0;
}

static void pause_ms(long ms) {
  struct timespec ts = {.tv_sec = ms / 1000, .tv_nsec = (ms % 1000) * 1000000L};
  while (nanosleep(&ts, &ts) < 0 && errno == EINTR) {}
}

static int exited_without_reaping(pid_t child, siginfo_t *info) {
  memset(info, 0, sizeof(*info));
  while (waitid(P_PID, (id_t)child, info, WEXITED | WNOHANG | WNOWAIT) < 0) {
    if (errno == EINTR) continue;
    return -1;
  }
  return info->si_pid == child;
}

static int create_control_fifo(const char *identity, char **fifo_out) {
  unsigned char nonce[16];
  int random_fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
  if (random_fd < 0) return -1;
  size_t offset = 0;
  while (offset < sizeof(nonce)) {
    ssize_t count = read(random_fd, nonce + offset, sizeof(nonce) - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) { close(random_fd); errno = EIO; return -1; }
    offset += (size_t)count;
  }
  close(random_fd);

  size_t identity_len = strlen(identity);
  const char suffix[] = ".control.";
  if (identity_len > SIZE_MAX - sizeof(suffix) - sizeof(nonce) * 2) {
    errno = ENAMETOOLONG;
    return -1;
  }
  size_t length = identity_len + sizeof(suffix) + sizeof(nonce) * 2;
  char *fifo = calloc(length, 1);
  if (!fifo) return -1;
  memcpy(fifo, identity, identity_len);
  memcpy(fifo + identity_len, suffix, sizeof(suffix) - 1);
  static const char hex[] = "0123456789abcdef";
  char *cursor = fifo + identity_len + sizeof(suffix) - 1;
  for (size_t i = 0; i < sizeof(nonce); i++) {
    cursor[i * 2] = hex[nonce[i] >> 4];
    cursor[i * 2 + 1] = hex[nonce[i] & 0x0f];
  }
  *fifo_out = fifo;
  return 0;
}

static bool fifo_matches_identity(const char *identity, const char *fifo) {
  size_t identity_len = strlen(identity);
  const char legacy_suffix[] = ".control";
  if (strcmp(fifo, "") == 0) return false;
  if (strlen(fifo) == identity_len + sizeof(legacy_suffix) - 1 &&
      strncmp(fifo, identity, identity_len) == 0 &&
      strcmp(fifo + identity_len, legacy_suffix) == 0) return true;
  const char suffix[] = ".control.";
  size_t prefix_len = identity_len + sizeof(suffix) - 1;
  if (strlen(fifo) != prefix_len + 32 || strncmp(fifo, identity, identity_len) != 0 ||
      strncmp(fifo + identity_len, suffix, sizeof(suffix) - 1) != 0) return false;
  for (size_t i = prefix_len; i < prefix_len + 32; i++) {
    char c = fifo[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  return true;
}

static void terminate_owned(pid_t child, bool group_owned, int signo) {
  siginfo_t info;
  int exited = exited_without_reaping(child, &info);
  if (exited == 0) (void)kill(group_owned ? -child : child, signo);
}

static int run_command(const char *identity, char **command) {
  char *fifo = NULL;
  if (create_control_fifo(identity, &fifo) < 0) {
    fprintf(stderr, "supervisor: cannot create unique control path: %s\n", strerror(errno));
    return 125;
  }
  if (mkfifo(fifo, 0600) < 0) {
    fprintf(stderr, "supervisor: cannot create control FIFO: %s\n", strerror(errno));
    free(fifo);
    return 125;
  }
  int control = open(fifo, O_RDWR | O_NONBLOCK | O_CLOEXEC);
  if (control < 0) {
    fprintf(stderr, "supervisor: cannot open control FIFO: %s\n", strerror(errno));
    unlink(fifo); free(fifo); return 125;
  }
  int record_fd = open(identity, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  FILE *record = record_fd >= 0 ? fdopen(record_fd, "w") : NULL;
  if (!record) {
    fprintf(stderr, "supervisor: cannot write identity: %s\n", strerror(errno));
    if (record_fd >= 0) close(record_fd);
    close(control); unlink(fifo); free(fifo); return 125;
  }

  struct sigaction child_action = {0};
  child_action.sa_handler = SIG_DFL;
  sigemptyset(&child_action.sa_mask);
  if (sigaction(SIGCHLD, &child_action, NULL) < 0) {
    fprintf(stderr, "supervisor: cannot reset SIGCHLD disposition: %s\n", strerror(errno));
    fclose(record); close(control); unlink(fifo); unlink(identity); free(fifo);
    return 125;
  }
  if (install_parent_signal_handlers() < 0) {
    fprintf(stderr, "supervisor: cannot install signal handlers: %s\n", strerror(errno));
    fclose(record); close(control); unlink(fifo); unlink(identity); free(fifo);
    return 125;
  }
  pid_t child = fork();
  if (child < 0) {
    fprintf(stderr, "supervisor: fork failed: %s\n", strerror(errno));
    fclose(record); close(control); unlink(fifo); unlink(identity); free(fifo);
    return 125;
  }
  if (child == 0) {
    if (restore_default_signal_handlers() < 0) _exit(125);
    if (setpgid(0, 0) < 0) _exit(125);
    execvp(command[0], command);
    fprintf(stderr, "supervisor: exec %s failed: %s\n", command[0], strerror(errno));
    _exit(errno == ENOENT ? 127 : 126);
  }
  (void)setpgid(child, child);
  bool group_owned = false;
  for (int attempt = 0; attempt < 20; attempt++) {
    if (getpgid(child) == child) { group_owned = true; break; }
    if (exited_without_reaping(child, &(siginfo_t){0}) != 0) break;
    pause_ms(5);
  }
  if (!group_owned)
    fprintf(stderr, "supervisor: child process group could not be verified; using direct-child cleanup only\n");
  fprintf(record, "pid=%ld\nchild_pid=%ld\ngroup_owned=%d\nfifo=%s\n",
          (long)getpid(), (long)child, group_owned ? 1 : 0, fifo);
  if (fclose(record) != 0) stop_requested = SIGTERM;

  bool stopping = false;
  long grace = 0;
  siginfo_t info;
  for (;;) {
    char request[64];
    ssize_t got = read(control, request, sizeof(request));
    if (got > 0) stop_requested = SIGTERM;
    int exited = exited_without_reaping(child, &info);
    if (exited < 0) { stop_requested = SIGTERM; }
    if (exited == 1 && !stopping) { stop_requested = SIGTERM; }
    if (stop_requested && !stopping) {
      stopping = true;
      terminate_owned(child, group_owned, (int)stop_requested);
      grace = 20;
    }
    if (stopping) {
      if (exited_without_reaping(child, &info) == 1) {
        /* The leader is still an unreaped zombie; its group ID cannot be reused. */
        if (group_owned) (void)kill(-child, SIGKILL);
        break;
      }
      if (grace-- <= 0) {
        terminate_owned(child, group_owned, SIGKILL);
        break;
      }
    }
    pause_ms(50);
  }

  int status = 0;
  while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
  close(control);
  unlink(fifo);
  unlink(identity);
  free(fifo);
  if (WIFEXITED(status)) return WEXITSTATUS(status);
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return 125;
}

static int stop_command(const char *identity) {
  int record_fd = open(identity, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  if (record_fd < 0) { perror("supervisor identity"); return 1; }
  FILE *record = fdopen(record_fd, "r");
  if (!record) {
    perror("supervisor identity stream");
    close(record_fd);
    return 1;
  }
  struct stat record_stat;
  if (fstat(fileno(record), &record_stat) < 0 || !S_ISREG(record_stat.st_mode) ||
      record_stat.st_uid != getuid() || (record_stat.st_mode & 077) != 0) {
    fclose(record);
    fprintf(stderr, "supervisor: refusing unsafe identity record\n");
    return 1;
  }
  char line[4096], fifo[PATH_MAX] = {0};
  while (fgets(line, sizeof(line), record)) {
    if (strncmp(line, "fifo=", 5) == 0) {
      line[strcspn(line, "\r\n")] = '\0';
      if (strlen(line + 5) < sizeof(fifo)) strcpy(fifo, line + 5);
    }
  }
  fclose(record);
  bool expected_path = fifo_matches_identity(identity, fifo);
  struct stat path_stat;
  if (!expected_path || !fifo[0] || lstat(fifo, &path_stat) < 0 ||
      !S_ISFIFO(path_stat.st_mode) || path_stat.st_uid != getuid() ||
      (path_stat.st_mode & 077) != 0) {
    fprintf(stderr, "supervisor: refusing stale or unsafe process identity\n");
    return 1;
  }
  int fd = open(fifo, O_WRONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) { perror("supervisor control"); return 1; }
  struct stat opened_stat;
  if (fstat(fd, &opened_stat) < 0 || !S_ISFIFO(opened_stat.st_mode) ||
      opened_stat.st_uid != getuid() || (opened_stat.st_mode & 077) != 0 ||
      opened_stat.st_dev != path_stat.st_dev || opened_stat.st_ino != path_stat.st_ino) {
    close(fd);
    fprintf(stderr, "supervisor: control FIFO changed during open\n");
    return 1;
  }
  ssize_t written = write(fd, "TERM", 4);
  close(fd);
  return written == 4 ? 0 : 1;
}

int main(int argc, char **argv) {
  if (argc >= 5 && strcmp(argv[1], "run") == 0 && strcmp(argv[3], "--") == 0)
    return run_command(argv[2], &argv[4]);
  if (argc == 3 && strcmp(argv[1], "stop") == 0) return stop_command(argv[2]);
  fprintf(stderr, "usage: %s run IDENTITY -- COMMAND [ARGS...] | stop IDENTITY\n", argv[0]);
  return 2;
}
