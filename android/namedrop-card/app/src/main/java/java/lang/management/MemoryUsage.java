package java.lang.management;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
public class MemoryUsage {
    private final long used, max;
    public MemoryUsage(long init, long used, long committed, long max) { this.used = used; this.max = max; }
    public long getUsed() { return used; }
    public long getMax() { return max; }
}
