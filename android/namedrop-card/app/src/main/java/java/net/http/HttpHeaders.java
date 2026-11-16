package java.net.http;
/* Android-compat shim: ART lacks this JDK API; Kwik/Flupke (the native NameDrop receiver) reference it.
 * Minimal, server-path-only. */
import java.util.*;
import java.util.function.BiPredicate;

/** Same contract as the JDK class: case-insensitive names, immutable, filtered on construction. */
public final class HttpHeaders {
    private final Map<String, List<String>> map;
    private HttpHeaders(Map<String, List<String>> m) { this.map = m; }

    public static HttpHeaders of(Map<String, List<String>> headerMap, BiPredicate<String, String> filter) {
        TreeMap<String, List<String>> m = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
        for (Map.Entry<String, List<String>> e : headerMap.entrySet()) {
            List<String> vs = new ArrayList<>();
            for (String v : e.getValue()) if (filter.test(e.getKey(), v)) vs.add(v);
            if (!vs.isEmpty()) m.merge(e.getKey(), Collections.unmodifiableList(vs), (a, b) -> {
                List<String> c = new ArrayList<>(a); c.addAll(b); return Collections.unmodifiableList(c); });
        }
        return new HttpHeaders(Collections.unmodifiableMap(m));
    }
    public Map<String, List<String>> map() { return map; }
    public List<String> allValues(String name) { List<String> v = map.get(name); return v == null ? List.of() : v; }
    public Optional<String> firstValue(String name) { return allValues(name).stream().findFirst(); }
    public OptionalLong firstValueAsLong(String name) {
        return firstValue(name).map(s -> OptionalLong.of(Long.parseLong(s))).orElse(OptionalLong.empty());
    }
    @Override public String toString() { return "HttpHeaders " + map; }
}
