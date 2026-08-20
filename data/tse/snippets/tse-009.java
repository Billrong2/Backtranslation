/**
     * Returns the elements in the base iterator until it hits any element that doesn't satisfy the filter.
     * Then the rest of the elements in the base iterator gets ignored.
     *
     * @since 1.485
     */
    public static <T> Iterator<T> limit(final Iterator<? extends T> base, final CountingPredicate<? super T> filter) {
        return new Iterator<T>() {
            private T next;
            private boolean end;
            private int index=0;
            public boolean hasNext() {
                fetch();
                return next!=null;
            }

            public T next() {
                fetch();
                T r = next;
                next = null;
                return r;
            }

            private void fetch() {
                if (next==null && !end) {
                    if (base.hasNext()) {
                        next = base.next();
                        if (!filter.apply(index++,next)) {
                            next = null;
                            end = true;
                        }
                    } else {
                        end = true;
                    }
                }
            }

            public void remove() {
                throw new UnsupportedOperationException();
            }
        };
    }