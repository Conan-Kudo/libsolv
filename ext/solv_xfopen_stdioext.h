#ifndef  _SOLV_XFOPEN_STDIOEXT_H
#define  _SOLV_XFOPEN_STDIOEXT_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdio.h>
#include <stdint.h>

typedef struct cookie_io_functions_t {
  ssize_t (*read)(void *cookie, char *buf, size_t n);
  ssize_t (*write)(void *cookie, const char *buf, size_t n);
  int (*seek)(void *cookie, off_t *pos, int whence);
  int (*close)(void *cookie);
} cookie_io_functions_t;

FILE *fopencookie(void *cookie, const char *mode, cookie_io_functions_t functions);

#ifdef __cplusplus
} // extern "C"
#endif

#endif  /* _SOLV_XFOPEN_STDIOEXT_H */
