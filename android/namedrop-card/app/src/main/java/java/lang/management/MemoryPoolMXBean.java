package java.lang.management;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
public interface MemoryPoolMXBean {
    String getName();
    MemoryType getType();
    MemoryUsage getUsage();
    MemoryUsage getCollectionUsage();
    boolean isUsageThresholdSupported();
    void setCollectionUsageThreshold(long threshold);
}
