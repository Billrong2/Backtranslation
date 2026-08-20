protected Chunk processChar(char[] cc, int k, StringBuffer sb) throws DocumentException, IOException {
    Chunk newChunk = null;
    char c = cc[k];
    if (c == '\n' || c == '\r') {
      sb.append(c);
    } else {
      Font font;
      if (Utilities.isSurrogatePair(cc, k)) {
        int u = Utilities.convertToUtf32(cc, k);
        for (int f = 0; f < files.length; ++f) {
          font = getFont(f);
          if (font.getBaseFont().charExists(u)
              || Character.getType(u) == Character.FORMAT) {
            if (currentFont != font) {
              if (sb.length() > 0 && currentFont != null) {
                newChunk = new Chunk(sb.toString(), currentFont);
                sb.setLength(0);
              }
              currentFont = font;
            }
            sb.append(c);
            sb.append(cc[++k]);
            break;
          }
        }
      } else {
        for (int f = 0; f < files.length; ++f) {
          font = getFont(f);
          if (font.getBaseFont().charExists(c)
              || Character.getType(c) == Character.FORMAT) {
            if (currentFont != font) {
              if (sb.length() > 0 && currentFont != null) {
                newChunk = new Chunk(sb.toString(), currentFont);
                sb.setLength(0);
              }
              currentFont = font;
            }
            sb.append(c);
            break;
          }
        }
      }
    }
    return newChunk;
  }