#include "ble.h"
#include <NimBLEDevice.h>

namespace net {

static const char *SVC_UUID = "177b0001-6f32-4ea3-b878-866e7628de1f";
static const char *ING_UUID = "177b0002-6f32-4ea3-b878-866e7628de1f";
static const char *OUT_UUID = "177b0003-6f32-4ea3-b878-866e7628de1f";

static volatile bool g_connected = false;
static NimBLECharacteristic *g_out = nullptr;

// NimBLE callbacks run on the NimBLE host task: only hand the bytes to the
// hub's queue (it copies them); parsing happens on the loop task.
class IngressCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c) override {
    NimBLEAttValue v = c->getValue();
    submit((const char *)v.data(), v.length(), SRC_BLE);
  }
};

class SrvCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *) override { g_connected = true; }
  void onDisconnect(NimBLEServer *) override { g_connected = false; }
};

static IngressCb g_ingressCb;
static SrvCb g_srvCb;

void BleTransport::begin() {
  NimBLEDevice::init("claude-cyd");
  NimBLEDevice::setMTU(517); // event envelopes (~400B) fit one write
  NimBLEServer *srv = NimBLEDevice::createServer();
  srv->setCallbacks(&g_srvCb);
  srv->advertiseOnDisconnect(true);
  NimBLEService *svc = srv->createService(SVC_UUID);
  NimBLECharacteristic *ing =
      svc->createCharacteristic(ING_UUID, NIMBLE_PROPERTY::WRITE);
  ing->setCallbacks(&g_ingressCb);
  g_out = svc->createCharacteristic(OUT_UUID, NIMBLE_PROPERTY::READ |
                                                  NIMBLE_PROPERTY::NOTIFY);
  g_out->setValue("{\"decision\":\"\"}");
  svc->start();
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->start();
  Serial.println("[ble] advertising as claude-cyd");
}

bool BleTransport::up(uint32_t) const { return g_connected; }

void BleTransport::send(const char *json) {
  if (!g_out)
    return;
  g_out->setValue(json);
  if (g_connected)
    g_out->notify();
}

} // namespace net
