/**
     * @see org.alfresco.jlan.server.filesys.NetworkFile#seekFile(long, int)
     */
    @Override
    public long seekFile(long pos, int typ) throws IOException {

        try {
            load(true);
            switch (typ) {

                //  From current position

                case SeekType.CurrentPos:
                    m_buffer.seek(m_buffer.getPosition() + pos);
                    break;

                //  From end of file

                case SeekType.EndOfFile:
                    long newPos = m_buffer.getLength() + pos;
                    m_buffer.seek(newPos);
                    break;

                //  From start of file

                case SeekType.StartOfFile:
                default:
                    m_buffer.seek(pos);
                    break;
            }
            return m_buffer.getPosition();
        } catch (CmsException e) {
            throw new IOException(e);
        }
    }