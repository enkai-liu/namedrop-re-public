package javax.management;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
public interface NotificationEmitter {
    void addNotificationListener(NotificationListener listener, NotificationFilter filter, Object handback);
}
