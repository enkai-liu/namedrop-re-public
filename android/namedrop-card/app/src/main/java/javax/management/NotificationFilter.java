package javax.management;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
public interface NotificationFilter { boolean isNotificationEnabled(Notification notification); }
