public boolean removeFast(T obj) {
		if (obj == null) {
			return false;
		}

		int b = getBucket(obj);
		T[] bucket = buckets[b];
		if ( bucket==null ) {
			// no bucket
			return false;
		}

		for (int i=0; i<bucket.length; i++) {
			T e = bucket[i];
			if ( e==null ) {
				// empty slot; not there
				return false;
			}

			if ( comparator.equals(e, obj) ) {          // found it
				// shift all elements to the right down one
				System.arraycopy(bucket, i+1, bucket, i, bucket.length-i-1);
				bucket[bucket.length - 1] = null;
				n--;
				return true;
			}
		}

		return false;
	}