import android.os.Binder;
import android.os.IBinder;
import android.os.Parcel;

/**
 * Raise (or drop) the Pixel's AWDL link by calling Google's IMoseyService directly, as root,
 * with no Quick Share UI. This replays what com.google.android.mosey sends when Quick Share
 * opens an AirDrop session, captured with Frida.
 *
 * Build: android/awdl-root/build.sh. Run on the device as root:
 *   CLASSPATH=/data/local/tmp/awdl-root.dex app_process / MoseyStart start <ch,..> <configHex> <holdSecs> [country]
 *   ... MoseyStart stop
 *   ... MoseyStart version
 *
 * Wire layout (from the decompiled AIDL stub, mosey 1.0.980159239):
 *   descriptor com.google.android.moseyservice.IMoseyService, service ".../default"
 *   TRANSACTION start=1 stop=2 update=3 queryMoseyHardwareCapability=4 getInterfaceVersion=0xFFFFFF
 *   typed object = int 1 (non-null) + stable-parcelable { int size, fields in order }
 *   StartMoseyParam  { int[] channels, IBinder binder, int maxSyntheticMdnsResponses, byte[] config }
 *   UpdateMoseyParam { String countryCode, byte[] config }
 *   StopMoseyParam   { }
 *
 * The binder is a plain Binder that Google uses as a death token, so the link should last only
 * while this process holds it: `start` sleeps for holdSecs, then calls stop and exits.
 */
public final class MoseyStart {
    static final String DESC = "com.google.android.moseyservice.IMoseyService";

    static IBinder service() throws Exception {
        // mosey_server is a `disabled oneshot` init service that exits once idle, so getService()
        // finds nothing between sessions. waitForDeclaredService() asks servicemanager to start
        // a declared lazy service and blocks until it registers — the path Google's app takes.
        Class<?> sm = Class.forName("android.os.ServiceManager");
        String name = DESC + "/default";
        IBinder b = (IBinder) sm.getMethod("getService", String.class).invoke(null, name);
        if (b == null) {
            System.out.println("not registered; waitForDeclaredService(" + name + ")");
            b = (IBinder) sm.getMethod("waitForDeclaredService", String.class).invoke(null, name);
        }
        if (b == null) throw new IllegalStateException(name + " not declared / did not start");
        return b;
    }

    interface Body { void write(Parcel p); }

    static Parcel call(IBinder svc, int code, Body body) throws Exception {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        d.writeInterfaceToken(DESC);
        if (body != null) {
            d.writeInt(1);                       // non-null typed object
            int start = d.dataPosition();
            d.writeInt(0);                       // size placeholder
            body.write(d);
            int end = d.dataPosition();
            d.setDataPosition(start);
            d.writeInt(end - start);
            d.setDataPosition(end);
        }
        boolean ok = svc.transact(code, d, r, 0);
        r.readException();
        d.recycle();
        System.out.println("transact " + code + " -> " + ok);
        return r;
    }

    static byte[] hex(String s) {
        byte[] b = new byte[s.length() / 2];
        for (int i = 0; i < b.length; i++) b[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        return b;
    }

    public static void main(String[] a) throws Exception {
        IBinder svc = service();
        switch (a[0]) {
            case "version": {
                Parcel r = call(svc, 0xFFFFFF, null);
                System.out.println("interface version " + r.readInt());
                break;
            }
            case "stop":
                call(svc, 2, p -> { });
                break;
            case "start": {
                String[] cs = a[1].split(",");
                int[] ch = new int[cs.length];
                for (int i = 0; i < cs.length; i++) ch[i] = Integer.parseInt(cs[i]);
                byte[] cfg = hex(a[2]);
                long hold = a.length > 3 ? Long.parseLong(a[3]) : 60;
                String country = a.length > 4 ? a[4] : "US";
                final Binder token = new Binder();
                // Google sends update(country, empty) immediately before start; do the same.
                call(svc, 3, p -> { p.writeString(country); p.writeByteArray(new byte[0]); });
                call(svc, 1, p -> {
                    p.writeIntArray(ch);
                    p.writeStrongBinder(token);
                    p.writeInt(Integer.MAX_VALUE);
                    p.writeByteArray(cfg);
                });
                System.out.println("START sent, holding token " + hold + " s");
                Thread.sleep(hold * 1000);
                call(svc, 2, p -> { });
                System.out.println("STOP sent");
                break;
            }
            default:
                System.out.println("usage: version | start <ch,..> <cfgHex> [holdSecs] [country] | stop");
        }
    }
}
