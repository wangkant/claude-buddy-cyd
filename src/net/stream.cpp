#include "stream.h"

namespace net {

bool LineReader::feed(int c) {
  if (done_) { // the previous call returned a line: start a fresh one
    n_ = 0;
    done_ = false;
  }
  if (c == '\n') {
    bool ok = !overflow_ && n_ > 0 && buf_[0] == '{';
    overflow_ = false;
    if (ok) {
      buf_[n_] = 0;
      done_ = true;
      return true;
    }
    n_ = 0;
    return false;
  }
  if (c == '\r')
    return false;
  if (n_ < sizeof(buf_) - 1)
    buf_[n_++] = (char)c;
  else
    overflow_ = true; // drop this whole line at its '\n'
  return false;
}

} // namespace net
