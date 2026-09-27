#include "home.h"
#include "layout.h"
#include "trends.h"
#include "app/ctx.h"
#include "app/activity.h"
#include "app/battery.h"
#include "net/hub.h"
#include "render/character.h"
#include "ui/text.h"
#include "ui/theme.h"
#include "ui/widgets.h"

using namespace app;
using namespace ui;

namespace screens {

static const int REG_Y = render::REG_Y, REG_H = render::REG_H; // single source

// rotating "busy" verb / idle line indices, advanced by the loop's sync signals
static int verbIdx = 0;
static int idleIdx = 0;

// bottom-card page: 0 = stats, 1 = trends
static int g_card = 0;
int card() { return g_card; }
void setCard(int c) { g_card = (c != 0) ? 1 : 0; }

// Animated (eased) copies of the live counters, so a value rolls toward its new
// total like an odometer instead of snapping.
static long long dToday = 0, dAll = 0;
static int dTools = 0, dTurns = 0, dSess = 0;
static uint32_t lastDurSec = 0xFFFFFFFF;

static bool tickToward(long long &d, long long target) {
  if (d == target) return false;
  long long step = (target - d) / 4; // ease-out roll
  if (step == 0) step = target > d ? 1 : -1;
  d += step;
  return true;
}
static bool tickTowardI(int &d, int target) {
  if (d == target) return false;
  int step = (target - d) / 4;
  if (step == 0) step = target > d ? 1 : -1;
  d += step;
  return true;
}

// Draw one centered value cell. Rendered via an off-screen sprite (blitText) so a
// rolling counter updates without flashing its cleared background each frame.
static void drawCell(int cx, int y, const char *v, uint16_t col,
                     const GFXfont *vf, int voff, int maxW) {
  blitText(cx - maxW / 2, y + voff - 1, maxW, 20, v, cx, y + voff, vf, col,
           C_CARD, TC_DATUM, maxW);
}

// The six value-cell strings, formatted from the animated counters in ONE place
// so the incremental painter and the full-page paint can't disagree.
struct StatText {
  char tok[12], all[12], dur[14], nt[8], nu[8], ns[8];
};
static void formatStats(StatText &o) {
  fmtTok(dToday, o.tok, sizeof(o.tok));
  fmtTok(dAll, o.all, sizeof(o.all));
  fmtDur(ctx.sessionStart ? (millis() - ctx.sessionStart) : 0, o.dur,
         sizeof(o.dur));
  snprintf(o.nt, sizeof(o.nt), "%d", dTools);
  snprintf(o.nu, sizeof(o.nu), "%d", dTurns);
  snprintf(o.ns, sizeof(o.ns), "%d", dSess);
}

// last-drawn text per stat cell: drawStatValues repaints only cells whose text
// changed; drawStatsPage syncs this on a full paint so the two stay coherent
static StatText prevStats;

// Draw the six value cells from the animated counters (labels are drawn once by
// renderStatic). force=true redraws all (after a full card repaint); otherwise
// only cells whose text changed are redrawn, so a settled value never flickers.
static void drawStatValues(int W, int cy, bool force) {
  StatText v;
  formatStats(v);
  const StatText &p = prevStats;
  int yA = cy + 54, yB = cy + 96;
  if (force || strcmp(v.tok, p.tok)) drawCell(W / 4, yA, v.tok, C_CORAL, &FreeSansBold12pt7b, 20, W / 2 - 24);
  if (force || strcmp(v.all, p.all)) drawCell(W * 3 / 4, yA, v.all, C_CORAL, &FreeSansBold12pt7b, 20, W / 2 - 24);
  if (force || strcmp(v.nt, p.nt)) drawCell(W / 8, yB, v.nt, C_TEXT, &FreeSansBold9pt7b, 15, W / 4 - 8);
  if (force || strcmp(v.nu, p.nu)) drawCell(W * 3 / 8, yB, v.nu, C_TEXT, &FreeSansBold9pt7b, 15, W / 4 - 8);
  if (force || strcmp(v.ns, p.ns)) drawCell(W * 5 / 8, yB, v.ns, C_TEXT, &FreeSansBold9pt7b, 15, W / 4 - 8);
  if (force || strcmp(v.dur, p.dur)) drawCell(W * 7 / 8, yB, v.dur, C_TEXT, &FreeSansBold9pt7b, 15, W / 4 - 8);
  prevStats = v;
}

void renderIntensity() {
  TFT_eSPI &t = tft();
  int W = t.width();
  t.fillRect(W - 56, 6, 36, 15, TFT_BLACK);
  for (int i = 0; i < ctx.intensity; i++)
    t.fillCircle(W - 30 - i * 11, 13, 3, C_CORAL);
}

// Battery glyph: outline + nub + fill in 5% buckets, between the label band
// and the intensity pips. Muted while healthy, amber under 20%, red under 10%
// (docs/battery-gauge-spec.md). It's an estimate -- no percent text up here;
// the exact figure lives in the Stats panel and the Settings row.
void renderBattery() {
  TFT_eSPI &t = tft();
  int W = t.width();
  int pct = app::battery::percent();
  uint16_t col = pct >= 20 ? C_MUTED : (pct >= 10 ? 0xFD20 : C_NO);
  int x = W - 82, y = 8, w = 17, h = 11;
  t.fillRect(x, y, w + 3, h, TFT_BLACK); // clear the cell (nub included)
  t.drawRoundRect(x, y, w, h, 2, col);
  t.fillRect(x + w + 1, y + 3, 2, h - 6, col); // battery nub
  int fw = (w - 4) * pct / 100;
  if (fw > 0)
    t.fillRect(x + 2, y + 2, fw, h - 4, col);
}

void renderBatteryIfChanged() {
  static int lastKey = -1;
  int pct = app::battery::percent();
  int key = (pct / 5) * 10 + (pct >= 20 ? 0 : (pct >= 10 ? 1 : 2));
  if (key == lastKey)
    return;
  lastKey = key;
  renderBattery();
}

// Daily-budget gauge: the card's divider becomes a thin progress bar when a
// budget is configured (coral -> amber near the cap -> red over). Uses the
// animated token count (dToday) so it eases with the odometer. cyAbs is the
// card's on-screen y; yOrg shifts it into canvas space (0 = the screen).
static void drawBudgetBar(TFT_eSPI &c, int W, int cyAbs, int yOrg,
                          const ui::CardPal &p) {
  net::AppState &s = net::hub.state();
  int bx = 20, bw = W - 40, by = cyAbs - yOrg + 38, bh = 4;
  c.fillRoundRect(bx, by, bw, bh, 2, p.divider); // track
  double frac = s.budget > 0 ? (double)dToday / (double)s.budget : 0;
  if (frac > 1)
    frac = 1;
  int fw = (int)(bw * frac);
  uint16_t col = frac >= 1.0 ? p.no : (frac > 0.85 ? p.amber : p.coral);
  if (fw > 0)
    c.fillRoundRect(bx, by, fw, bh, 2, col);
}

// Card headline text: while busy, the device-rotated whimsy verb (synced to the
// animation); otherwise the hook activity msg, else project, else name.
static const char *headlineText(const char *st) {
  net::AppState &s = net::hub.state();
  if (!strcmp(st, "busy"))
    return WHIMSY[verbIdx];
  if (isWork(st)) // tool-specific activity -> its own verb
    return actVerb(st);
  // transient / reaction states get their own word so the headline always
  // matches the animation above it (not a stale activity message)
  if (!strcmp(st, "dizzy")) return "Whoa!";
  if (!strcmp(st, "celebrate")) return "Nice work!";
  if (!strcmp(st, "heart")) return "Hello!";
  if (!strcmp(st, "error")) return "Oops...";
  if (!strcmp(st, "attention") || !strcmp(st, "notification"))
    return "Needs you";
  if (!strcmp(st, "idle")) // standing by -> a gently rotating friendly line
    return IDLE_MSGS[idleIdx];
  if (s.msg.length())
    return s.msg.c_str();
  if (s.project.length())
    return s.project.c_str();
  return "Claude Buddy";
}

// The full stats page (card bg, headline, divider/budget, labels, values) onto
// any canvas. yOrg translates screen-y into canvas space (0 = the screen
// itself). Direct draws only -- the caller provides a fresh background, so
// nothing here needs the anti-flicker blit path. live=true also syncs the
// incremental painters' caches so post-paint rolling updates stay coherent.
void drawStatsPage(TFT_eSPI &c, int yOrg, const char *st,
                   const ui::CardPal &p, bool live) {
  net::AppState &s = net::hub.state();
  int W = tft().width(), H = tft().height();
  int cyA = REG_Y + REG_H + 4; // absolute card y (layout is screen geometry)
  int cy = cyA - yOrg;
  int chh = H - cyA - 6;
  c.fillRoundRect(8, cy, W - 16, chh, 12, p.card);
  gtextClampC(c, headlineText(st), W / 2, cy + 18, &FreeSansBold12pt7b, p.text,
              p.card, MC_DATUM, W - 32);
  if (s.budget > 0)
    drawBudgetBar(c, W, cyA, yOrg, p);
  else
    c.drawFastHLine(20, cy + 40, W - 40, p.divider);
  int yA = cy + 54, yB = cy + 96;
  gtextC(c, "Today", W / 4, yA, &FreeSans9pt7b, p.muted, p.card, TC_DATUM);
  gtextC(c, "Total", W * 3 / 4, yA, &FreeSans9pt7b, p.muted, p.card, TC_DATUM);
  gtextC(c, "Tools", W / 8, yB, &FreeSans9pt7b, p.muted, p.card, TC_DATUM);
  gtextC(c, "Turns", W * 3 / 8, yB, &FreeSans9pt7b, p.muted, p.card, TC_DATUM);
  gtextC(c, "Sess", W * 5 / 8, yB, &FreeSans9pt7b, p.muted, p.card, TC_DATUM);
  gtextC(c, "Time", W * 7 / 8, yB, &FreeSans9pt7b, p.muted, p.card, TC_DATUM);
  StatText v;
  formatStats(v);
  // same anchors as drawCell: TC_DATUM at y+20 (12pt values) / y+15 (9pt)
  gtextC(c, v.tok, W / 4, yA + 20, &FreeSansBold12pt7b, p.coral, p.card,
         TC_DATUM);
  gtextC(c, v.all, W * 3 / 4, yA + 20, &FreeSansBold12pt7b, p.coral, p.card,
         TC_DATUM);
  gtextC(c, v.nt, W / 8, yB + 15, &FreeSansBold9pt7b, p.text, p.card,
         TC_DATUM);
  gtextC(c, v.nu, W * 3 / 8, yB + 15, &FreeSansBold9pt7b, p.text, p.card,
         TC_DATUM);
  gtextC(c, v.ns, W * 5 / 8, yB + 15, &FreeSansBold9pt7b, p.text, p.card,
         TC_DATUM);
  gtextC(c, v.dur, W * 7 / 8, yB + 15, &FreeSansBold9pt7b, p.text, p.card,
         TC_DATUM);
  if (live)
    prevStats = v;
}

void renderHeadline(const char *st) {
  if (g_card != 0)
    return; // the headline band belongs to the stats card page
  int W = tft().width(), cy = REG_Y + REG_H + 4;
  // sprite-blit the headline band so the rotating verb/idle line never flashes.
  blitText(12, cy + 5, W - 24, 28, headlineText(st), W / 2, cy + 18,
           &FreeSansBold12pt7b, C_TEXT, C_CARD, MC_DATUM, W - 32);
}

// The label band stops short of the intensity pips / link dot, which update on
// their own.
void renderStatusBar(const char *st) {
  TFT_eSPI &t = tft();
  net::AppState &s = net::hub.state();
  int W = t.width();
  t.fillCircle(13, 13, 5, stateColor(st));
  blitText(26, 2, (W - 84) - 26, 24, stateLabel(st), 26, 14, &FreeSansBold9pt7b,
           C_TEXT, TFT_BLACK, ML_DATUM, (W - 84) - 26);
  t.fillCircle(W - 13, 13, 4, s.linkUp ? 0x2DEA : 0x9000);
}

void renderStatic(const char *st) {
  TFT_eSPI &t = tft();
  net::AppState &s = net::hub.state();
  int W = t.width(), H = t.height();

  // top status bar
  t.fillRect(0, 0, W, REG_Y, TFT_BLACK);
  t.fillCircle(13, 13, 5, stateColor(st));
  gtext(stateLabel(st), 26, 14, &FreeSansBold9pt7b, C_TEXT, TFT_BLACK, ML_DATUM);
  t.fillCircle(W - 13, 13, 4, s.linkUp ? 0x2DEA : 0x9000); // link indicator
  renderIntensity(); // session-intensity pips
  renderBattery();   // battery gauge glyph

  // bottom stats card (just below the character region)
  t.fillRect(0, REG_Y + REG_H, W, H - (REG_Y + REG_H), TFT_BLACK);
  // On the needs-you screen, drop the stats card -> a clean alert: just the amber
  // Clawd and a "Got it" button to dismiss. Drawn ONCE here (the lower area is
  // static -- the animation stays in the region above it -- so no per-frame
  // repaint is needed, which is what used to make the button flicker).
  if (!strcmp(st, "attention") || !strcmp(st, "notification")) {
    // coral fill so it sits in the warm needs-you palette (the brand accent)
    // rather than the out-of-place Allow-green it used before
    drawButton(ackBtn, "Got it", C_CORAL);
    return;
  }
  if (g_card == 1) { // trends page: same card slot, different content
    renderTrends(true);
    return;
  }
  drawStatsPage(t, 0, st, ui::PAL_RGB, true);
}

void seedStats() {
  net::AppState &s = net::hub.state();
  dToday = s.tokens;
  dAll = s.tokensAll;
  dTools = s.tools;
  dTurns = s.turns;
  dSess = s.sessions;
}

void rollStats(uint32_t now, const char *st) {
  net::AppState &s = net::hub.state();
  static uint32_t lastRoll = 0;
  if (now - lastRoll <= 40)
    return;
  lastRoll = now;
  bool ch = false;
  ch |= tickToward(dToday, s.tokens);
  ch |= tickToward(dAll, s.tokensAll);
  ch |= tickTowardI(dTools, s.tools);
  ch |= tickTowardI(dTurns, s.turns);
  ch |= tickTowardI(dSess, s.sessions);
  uint32_t durSec = ctx.sessionStart ? (now - ctx.sessionStart) / 1000 : 0;
  if (durSec != lastDurSec) {
    lastDurSec = durSec;
    ch = true;
  }
  // no card on the needs-you screen -> don't paint stat cells into the void
  if (!strcmp(st, "attention") || !strcmp(st, "notification"))
    return;
  if (g_card == 1) { // trends page: keep today's bar growing instead
    renderTrends(false);
    return;
  }
  if (ch) {
    int W = tft().width();
    drawStatValues(W, REG_Y + REG_H + 4, false);
    if (s.budget > 0) // ease the gauge with the counter
      drawBudgetBar(tft(), W, REG_Y + REG_H + 4, 0, ui::PAL_RGB);
  }
}

void rotateVerb() { verbIdx = (verbIdx + 1) % N_WHIMSY; }

void rotateIdle() { idleIdx = (idleIdx + 1) % N_IDLE; }

} // namespace screens
