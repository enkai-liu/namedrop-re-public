package javax.management;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
public class Notification {
    private final String type;
    public Notification(String type, Object source, long seq) { this.type = type; }
    public String getType() { return type; }
}
