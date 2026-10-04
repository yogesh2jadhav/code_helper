"""A small registry of well-known JDK types.

The analyzer has no access to the JDK's class files, so this lets `import java.util.*` or a bare
`String` resolve to a fully qualified name without guessing. It is deliberately incomplete: a name
missing here stays *unresolved* (with candidates) rather than being assumed.
"""

from __future__ import annotations

PRIMITIVES = frozenset(
    {"boolean", "byte", "short", "int", "long", "char", "float", "double", "void"}
)

# simple name of a java.lang box -> primitive
BOXES = {
    "Boolean": "boolean",
    "Byte": "byte",
    "Short": "short",
    "Integer": "int",
    "Long": "long",
    "Character": "char",
    "Float": "float",
    "Double": "double",
}

# primitive widening: from -> set of types it converts to (JLS 5.1.2)
WIDENING = {
    "byte": {"short", "int", "long", "float", "double"},
    "short": {"int", "long", "float", "double"},
    "char": {"int", "long", "float", "double"},
    "int": {"long", "float", "double"},
    "long": {"float", "double"},
    "float": {"double"},
}

# JDK value types that cannot be subclassed, so two different ones are never assignable
FINAL_VALUE_TYPES = frozenset({"java.lang.String"} | {f"java.lang.{name}" for name in BOXES})

OBJECT_METHODS = frozenset(
    {
        "equals",
        "hashCode",
        "toString",
        "getClass",
        "notify",
        "notifyAll",
        "wait",
        "clone",
        "finalize",
    }
)

LOMBOK_ANNOTATIONS = frozenset(
    {
        "Data",
        "Getter",
        "Setter",
        "Builder",
        "Value",
        "AllArgsConstructor",
        "NoArgsConstructor",
        "RequiredArgsConstructor",
        "ToString",
        "EqualsAndHashCode",
        "With",
        "SuperBuilder",
        "Slf4j",
        "Log4j2",
        "Log",
        "CommonsLog",
        "Accessors",
        "Singular",
    }
)

_PACKAGES = {
    "java.lang": (
        "Object String StringBuilder StringBuffer Math StrictMath System Integer Long Double Float "
        "Short Byte Character Boolean Void Number Class ClassLoader Thread Runnable Runtime Process "
        "ProcessBuilder Iterable Comparable CharSequence AutoCloseable Cloneable Enum Record "
        "Exception RuntimeException Error Throwable AssertionError IllegalArgumentException "
        "IllegalStateException NullPointerException ArithmeticException "
        "ArrayIndexOutOfBoundsException IndexOutOfBoundsException ClassCastException "
        "NumberFormatException UnsupportedOperationException InterruptedException "
        "CloneNotSupportedException ClassNotFoundException StackOverflowError OutOfMemoryError "
        "SecurityException Override Deprecated SuppressWarnings FunctionalInterface SafeVarargs "
        "StackTraceElement ThreadLocal"
    ),
    "java.util": (
        "List ArrayList LinkedList Map HashMap LinkedHashMap TreeMap Set HashSet LinkedHashSet "
        "TreeSet SortedMap SortedSet NavigableMap NavigableSet Collection Collections Arrays Objects "
        "Optional OptionalInt OptionalLong OptionalDouble Iterator ListIterator Queue Deque "
        "ArrayDeque PriorityQueue Stack Vector Hashtable Comparator Date Calendar GregorianCalendar "
        "Locale Random Scanner StringJoiner UUID BitSet Properties Timer TimerTask EnumMap EnumSet "
        "IdentityHashMap WeakHashMap NoSuchElementException ConcurrentModificationException "
        "Currency Base64 AbstractList AbstractMap AbstractSet AbstractCollection "
        "IntSummaryStatistics LongSummaryStatistics DoubleSummaryStatistics Formatter "
        "StringTokenizer TimeZone Spliterator"
    ),
    "java.util.function": (
        "Function BiFunction Supplier Consumer BiConsumer Predicate BiPredicate UnaryOperator "
        "BinaryOperator IntFunction IntPredicate IntUnaryOperator IntBinaryOperator ToIntFunction "
        "ToLongFunction ToDoubleFunction IntSupplier BooleanSupplier LongFunction DoubleFunction "
        "IntConsumer"
    ),
    "java.util.stream": "Stream IntStream LongStream DoubleStream Collectors Collector StreamSupport",
    "java.util.concurrent": (
        "ConcurrentHashMap ConcurrentMap CopyOnWriteArrayList ExecutorService Executors Future "
        "CompletableFuture Callable TimeUnit CountDownLatch Semaphore ThreadPoolExecutor "
        "ScheduledExecutorService ConcurrentLinkedQueue BlockingQueue LinkedBlockingQueue "
        "ExecutionException TimeoutException"
    ),
    "java.util.concurrent.atomic": "AtomicInteger AtomicLong AtomicBoolean AtomicReference",
    "java.util.concurrent.locks": "Lock ReentrantLock ReadWriteLock ReentrantReadWriteLock",
    "java.util.regex": "Pattern Matcher",
    "java.time": (
        "LocalDate LocalDateTime LocalTime Instant Duration Period ZonedDateTime ZoneId ZoneOffset "
        "OffsetDateTime Year YearMonth DayOfWeek Month Clock"
    ),
    "java.time.format": "DateTimeFormatter DateTimeParseException",
    "java.time.temporal": "ChronoUnit TemporalAdjusters ChronoField",
    "java.io": (
        "File IOException InputStream OutputStream Reader Writer BufferedReader BufferedWriter "
        "FileReader FileWriter InputStreamReader OutputStreamWriter PrintStream PrintWriter "
        "Serializable Closeable ByteArrayInputStream ByteArrayOutputStream FileInputStream "
        "FileOutputStream UncheckedIOException FileNotFoundException ObjectInputStream "
        "ObjectOutputStream"
    ),
    "java.nio.file": "Files Path Paths StandardOpenOption",
    "java.nio.charset": "StandardCharsets Charset",
    "java.math": "BigDecimal BigInteger RoundingMode MathContext",
    "java.text": "SimpleDateFormat DecimalFormat NumberFormat DateFormat MessageFormat ParseException",
    "java.net": "URL URI URLEncoder URLDecoder HttpURLConnection",
    "java.sql": (
        "Connection PreparedStatement ResultSet Statement SQLException Timestamp Date Time DriverManager"
    ),
}

KNOWN_JDK_TYPES: dict[str, frozenset[str]] = {
    pkg: frozenset(names.split()) for pkg, names in _PACKAGES.items()
}
JAVA_LANG = KNOWN_JDK_TYPES["java.lang"]

# Well-known JDK static fields whose type matters for chained calls (`System.out.println(..)`).
KNOWN_STATIC_FIELD_TYPES = {
    ("java.lang.System", "out"): "java.io.PrintStream",
    ("java.lang.System", "err"): "java.io.PrintStream",
    ("java.lang.System", "in"): "java.io.InputStream",
}
