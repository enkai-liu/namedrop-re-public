package java.lang.management;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
public final class ManagementFactory {
    private ManagementFactory() {}
    private static final MemoryMXBean BEAN = new Bean();
    public static MemoryMXBean getMemoryMXBean() { return BEAN; }
    /** No pools: Kwik's MemoryWatcher then registers nothing and never fires. */
    public static java.util.List<MemoryPoolMXBean> getMemoryPoolMXBeans() { return java.util.Collections.emptyList(); }
    private static final class Bean implements MemoryMXBean, javax.management.NotificationEmitter {
        @Override public void addNotificationListener(javax.management.NotificationListener l,
                javax.management.NotificationFilter f, Object handback) { }
    }
}
