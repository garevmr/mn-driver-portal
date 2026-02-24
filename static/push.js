async function registerSW() {
  if (!('serviceWorker' in navigator)) return null;
  return navigator.serviceWorker.register('/static/sw.js');
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
  return outputArray;
}

async function subscribePush(vapidPublicKey) {
  const reg = await registerSW();
  if (!reg) throw new Error("Service Worker not supported");

  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error("Notifications permission not granted");

  const existing = await reg.pushManager.getSubscription();
  if (existing) return existing;

  return reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
  });
}

async function postJSON(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || "Request failed");
  }
  return res.json().catch(() => ({}));
}

window.MNPush = {
  enable: async function(vapidPublicKey){
    const sub = await subscribePush(vapidPublicKey);
    await postJSON('/push/subscribe', sub);
    return true;
  },
  disable: async function(){
    const reg = await registerSW();
    if (!reg) return true;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await postJSON('/push/unsubscribe', sub);
      await sub.unsubscribe();
    }
    return true;
  }
};
